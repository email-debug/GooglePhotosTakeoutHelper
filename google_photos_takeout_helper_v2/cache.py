"""
Incremental run cache manager.

Handles reading/writing takeout_log.json for safe incremental runs.
Supports:
- Work/final log path separation (write to temp, move to final on success)
- Periodic saves every N files (configurable, default 500)
- Atomic writes via tmp+rename
- Cache validation (input_dir match, completion flag)
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from .config import CACHE_SAVE_INTERVAL, LOG_FILENAME
from .types import FileState


def _now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def resolve_log_paths(
    final_log_dir: str,
    temp_log_dir: Optional[str] = None,
) -> Tuple[Path, Path, bool]:
    """
    Resolve work and final log file paths.

    If temp_log_dir is set and different from final: work in temp, final is separate.
    Otherwise: work and final are the same path.

    Returns:
        (work_log_path, final_log_path, use_temp: bool)
    """
    final_dir = Path(final_log_dir)
    final_log_path = final_dir / LOG_FILENAME

    if temp_log_dir is not None and str(temp_log_dir) != str(final_log_dir):
        work_dir = Path(temp_log_dir)
        work_log_path = work_dir / LOG_FILENAME
        return (work_log_path, final_log_path, True)

    return (final_log_path, final_log_path, False)


def _get_load_path(work_log_path: Path, final_log_path: Path, use_temp: bool) -> Optional[Path]:
    """Determine which log file to load: work first (if temp), then final."""
    if use_temp and work_log_path.exists():
        return work_log_path
    if final_log_path.exists():
        return final_log_path
    return None


class CacheManager:
    """
    Manages the incremental run cache (takeout_log.json).

    Usage:
        cache = CacheManager(final_log_dir, temp_log_dir)
        cache.load()
        # ... process files ...
        cache.mark_file_done(rel_path, file_state)
        cache.save_if_needed()  # periodic
        cache.save_final()      # at end
        cache.move_to_final()   # if using temp
    """

    def __init__(self, final_log_dir: str, temp_log_dir: Optional[str] = None):
        self.work_path, self.final_path, self.use_temp = resolve_log_paths(
            final_log_dir, temp_log_dir
        )
        self._data: dict = {}
        self._file_states: Dict[str, FileState] = {}
        self._dirty = False
        self._processed_count = 0
        self._save_interval = CACHE_SAVE_INTERVAL

    def load(self) -> bool:
        """
        Load cache from disk.

        Returns True if a valid cache was loaded, False if starting fresh.
        """
        load_path = _get_load_path(self.work_path, self.final_path, self.use_temp)
        if load_path is None:
            self._data = {}
            self._file_states = {}
            return False

        try:
            with open(load_path, 'r', encoding='utf-8') as f:
                self._data = json.load(f)
        except Exception as e:
            logger.warning(f'[cache] Could not read cache: {e}')
            self._data = {}
            self._file_states = {}
            return False

        # Rebuild FileState objects from raw dicts
        raw_states = self._data.get('file_states', {})
        self._file_states = {
            k: FileState.from_dict(v) if isinstance(v, dict) else FileState()
            for k, v in raw_states.items()
        }
        return True

    def validate_for_input(self, input_dir: str) -> bool:
        """Check if cache was built from the same input directory."""
        return self._data.get('input_dir') == input_dir

    @property
    def is_completed(self) -> bool:
        """Whether the cached run completed successfully."""
        return bool(self._data.get('completed'))

    @property
    def file_states(self) -> Dict[str, FileState]:
        return self._file_states

    @property
    def data(self) -> dict:
        return self._data

    def get_already_done_keys(self) -> set:
        """Return set of relative paths for files already moved in a prior run."""
        return {
            k for k, v in self._file_states.items()
            if v.moved
        }

    def get_already_exif_keys(self) -> set:
        """Return set of relative paths for files with EXIF updated but not moved."""
        return {
            k for k, v in self._file_states.items()
            if v.exif_updated and not v.moved
        }

    def mark_file_state(self, rel_path: str, state: FileState):
        """Update the state for a file and mark cache dirty."""
        self._file_states[rel_path] = state
        self._dirty = True
        self._processed_count += 1

    def get_file_state(self, rel_path: str) -> Optional[FileState]:
        """Get the state for a file, or None if not tracked."""
        return self._file_states.get(rel_path)

    def save_if_needed(self, force=False):
        """Save cache periodically based on CACHE_SAVE_INTERVAL."""
        if force or (self._dirty and self._processed_count % self._save_interval == 0):
            self._write_cache()

    def save_final(self, summary: Optional[dict] = None):
        """Save final cache state with completion flag."""
        if summary:
            self._data['last_run_summary'] = summary
        self._data['completed'] = True
        self._data['completed_at'] = _now_iso()
        self._write_cache()

    def set_json_index(self, index_data: dict, input_dir: str, media_count: int):
        """Store the JSON index in cache for fast resumption."""
        self._data['json_index'] = index_data
        self._data['input_dir'] = input_dir
        self._data['media_file_count'] = media_count
        self._write_cache()

    def get_json_index(self) -> Optional[dict]:
        """Get cached JSON index, or None."""
        return self._data.get('json_index')

    def get_media_file_count(self) -> int:
        """Get cached media file count."""
        return self._data.get('media_file_count', -1)

    def move_to_final(self):
        """Move work log to final location (if using temp)."""
        if self.use_temp and self.work_path.exists():
            self.final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.work_path), str(self.final_path))
            logger.info(f'[cache] Moved to final: {self.final_path}')

    def _write_cache(self):
        """Write cache atomically (write to .tmp then rename)."""
        try:
            self.work_path.parent.mkdir(parents=True, exist_ok=True)

            # Serialize file states
            self._data['file_states'] = {
                k: v.to_dict() for k, v in self._file_states.items()
            }
            self._data['last_save'] = _now_iso()

            tmp_path = self.work_path.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2, default=str)
            tmp_path.replace(self.work_path)
            self._dirty = False
        except Exception as e:
            logger.warning(f'[cache] Could not save: {e}')

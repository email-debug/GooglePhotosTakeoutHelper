"""
Type definitions and dataclasses for the v2 takeout helper.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class FileState:
    """Per-file state tracked in the incremental cache."""
    matched: bool = False
    match_type: str = 'unknown'
    json_path: Optional[str] = None
    got_date: bool = False
    got_geo: bool = False
    exif_updated: bool = False
    moved: bool = False
    dest_path: Optional[str] = None
    deleted_source: bool = False
    copied_to_staging: Optional[str] = None
    source_hash: Optional[str] = None  # SHA-1 hex digest of original source bytes

    def to_dict(self) -> dict:
        return {
            'matched': self.matched,
            'match_type': self.match_type,
            'json_path': self.json_path,
            'got_date': self.got_date,
            'got_geo': self.got_geo,
            'exif_updated': self.exif_updated,
            'moved': self.moved,
            'dest_path': self.dest_path,
            'deleted_source': self.deleted_source,
            'copied_to_staging': self.copied_to_staging,
            'source_hash': self.source_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'FileState':
        return cls(
            matched=d.get('matched', False),
            match_type=d.get('match_type', 'unknown'),
            json_path=d.get('json_path'),
            got_date=d.get('got_date', False),
            got_geo=d.get('got_geo', False),
            exif_updated=d.get('exif_updated', False),
            moved=d.get('moved', False),
            dest_path=d.get('dest_path'),
            deleted_source=d.get('deleted_source', False),
            copied_to_staging=d.get('copied_to_staging'),
            source_hash=d.get('source_hash'),
        )


@dataclass
class MatchResult:
    """Result of matching a media file to its JSON sidecar."""
    json_path: Optional[Path] = None
    match_type: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.json_path is not None


@dataclass
class EnrichmentInfo:
    """Metadata extracted when fixing EXIF from a JSON sidecar."""
    json_path: Optional[str] = None
    got_date: bool = False
    got_geo: bool = False


@dataclass
class RunStats:
    """
    Comprehensive statistics for a single run.
    All counters are explicit — no numbers are inferred or computed from other counters.
    This fixes the v1 bug where output numbers didn't add up.
    """
    # Input scanning
    input_media_files: int = 0
    input_json_files: int = 0

    # Processing outcomes (mutually exclusive per file)
    copied_new: int = 0              # Files copied to output this run
    skipped_already_done: int = 0    # Files skipped because already in output from prior run
    skipped_extras: int = 0          # Files skipped due to --skip-extras
    recopied_missing_dest: int = 0   # Files whose dest was missing and were re-copied

    # Source deletion (subset of copied_new + skipped_already_done)
    source_deleted: int = 0

    # EXIF outcomes
    exif_from_json: int = 0
    exif_from_existing: int = 0
    exif_from_folder_meta: int = 0
    exif_from_filename: int = 0
    exif_from_folder_year: int = 0
    exif_write_failed: int = 0
    no_date_at_all: int = 0

    # JSON sidecar matching
    json_matched: int = 0
    json_not_found: int = 0

    # GPS
    geo_set: int = 0

    # Deduplication (post-copy)
    duplicates_found: int = 0
    duplicates_removed: int = 0

    # Albums
    albums_created: int = 0
    album_entries: int = 0

    # Output verification
    output_file_count: int = 0

    # Problem files (for detailed logs)
    no_json_files: List[str] = field(default_factory=list)
    no_date_files: List[str] = field(default_factory=list)
    exif_failed_files: List[str] = field(default_factory=list)
    skipped_extra_files: List[str] = field(default_factory=list)
    date_from_folder_files: List[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        """Total files that went through the processing pipeline."""
        return self.copied_new + self.skipped_already_done + self.recopied_missing_dest

    @property
    def total_in_output(self) -> int:
        """Expected files in output = all moved - duplicates removed."""
        return (self.copied_new + self.skipped_already_done + self.recopied_missing_dest
                - self.duplicates_removed)

    def verify_counts(self) -> List[str]:
        """
        Cross-check counters for consistency.
        Returns list of warnings if anything doesn't add up.
        """
        warnings = []
        # Every processed file should have exactly one EXIF outcome
        exif_total = (self.exif_from_json + self.exif_from_existing +
                      self.exif_from_folder_meta + self.exif_from_filename +
                      self.exif_from_folder_year + self.no_date_at_all)
        processed = self.copied_new + self.recopied_missing_dest
        if exif_total != processed and processed > 0:
            warnings.append(
                f"EXIF outcomes ({exif_total:,}) != files processed ({processed:,}). "
                f"Breakdown: json={self.exif_from_json}, existing={self.exif_from_existing}, "
                f"folder_meta={self.exif_from_folder_meta}, filename={self.exif_from_filename}, "
                f"folder_year={self.exif_from_folder_year}, none={self.no_date_at_all}"
            )
        # JSON match outcomes should cover all processed files
        json_total = self.json_matched + self.json_not_found
        if json_total != processed and processed > 0:
            warnings.append(
                f"JSON outcomes ({json_total:,}) != files processed ({processed:,})"
            )
        return warnings

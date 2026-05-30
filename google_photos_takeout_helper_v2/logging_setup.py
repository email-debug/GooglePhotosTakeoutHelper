"""
Logging configuration for the v2 takeout helper.

Uses loguru for structured logging with:
- Console output: INFO+ through tqdm.write (avoids corrupting progress bars)
- File output: TRACE+ to timestamped log files
- Bounded log rotation: max 5 log files, 50MB each (fixes v1's unbounded log growth)
"""
import sys
from loguru import logger
from tqdm import tqdm


def setup_logging(log_dir=None):
    """
    Configure loguru for the takeout helper.

    Args:
        log_dir: Directory for log files. If None, logs go to current directory.
    """
    # Ensure stdout can handle unicode
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')

    # Remove default handler
    logger.remove()

    # Console: INFO+ through tqdm.write to avoid corrupting progress bars
    logger.add(
        lambda msg: tqdm.write(msg, end=""),
        format="{message}",
        level="INFO",
    )

    # File: TRACE+ with rotation to prevent unbounded log growth
    # v1 created a new log file per run with no cleanup — this fixes that
    log_pattern = "gpth_{time}.log"
    if log_dir:
        from pathlib import Path
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_pattern = str(Path(log_dir) / log_pattern)

    logger.add(
        log_pattern,
        level="TRACE",
        encoding="utf8",
        rotation="50 MB",
        retention=5,
    )

    return logger

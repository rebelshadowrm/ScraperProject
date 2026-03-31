# common/logging_utils.py

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

class ConsoleProgressFilter(logging.Filter):
    """Allow warnings/errors through, plus explicitly marked progress records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or getattr(record, "progress", False)


def log_progress(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    extra = kwargs.pop("extra", {})
    extra["progress"] = True
    logger.info(msg, *args, extra=extra, **kwargs)


def build_run_log_path(run_label: str = "run", logs_root: str = "Logs") -> str:
    now = datetime.now()
    date_dir = Path(logs_root) / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (run_label or "run"))
    return str(date_dir / f"{now.strftime('%H%M%S')}_{safe_label}.log")


def cleanup_old_logs(logs_root: str = "Logs", *, keep_runs: int = 50, keep_days: int = 60) -> None:
    root = Path(logs_root)
    if not root.exists():
        return

    cutoff = datetime.now() - timedelta(days=keep_days)
    log_files = sorted(
        [p for p in root.rglob("*.log") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    kept = []
    to_delete = []

    for idx, path in enumerate(log_files):
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if idx < keep_runs or mtime >= cutoff:
            kept.append(path)
        else:
            to_delete.append(path)

    for path in to_delete:
        try:
            path.unlink()
        except Exception:
            continue

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        try:
            if not dirnames and not filenames:
                Path(dirpath).rmdir()
        except Exception:
            pass


def setup_logger(log_file="scraping.log", log_level=logging.INFO):
    """
    Set up and return a root logger that writes to both a file and stdout.
    """
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Remove any existing handlers so we don't duplicate logs
    if logger.hasHandlers():
        logger.handlers.clear()

    # File Handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Stream Handler (stdout)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(log_level)
    stream_handler.addFilter(ConsoleProgressFilter())
    stream_formatter = logging.Formatter('%(levelname)s - %(message)s')
    stream_handler.setFormatter(stream_formatter)
    logger.addHandler(stream_handler)

    # Adjust WebDriver Manager logging (optional, to reduce verbosity)
    wdm_logger = logging.getLogger("WDM")
    wdm_logger.setLevel(logging.WARNING)

    log_progress(logger, "Logger initialized.")
    return logger

def flush_logs():
    """
    Flush the logs on all file handlers to ensure everything
    is written to disk immediately.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

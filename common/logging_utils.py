# common/logging_utils.py

import logging
import sys

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
    stream_formatter = logging.Formatter('%(levelname)s - %(message)s')
    stream_handler.setFormatter(stream_formatter)
    logger.addHandler(stream_handler)

    # Adjust WebDriver Manager logging (optional, to reduce verbosity)
    wdm_logger = logging.getLogger("WDM")
    wdm_logger.setLevel(logging.WARNING)

    logger.info("Logger initialized.")
    return logger

def flush_logs():
    """
    Flush the logs on all file handlers to ensure everything
    is written to disk immediately.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

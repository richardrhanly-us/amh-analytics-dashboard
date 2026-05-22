#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         logger_config.py
#
#  Description: Provides shared logging configuration for the
#               SortView AMH pipeline. This file creates a reusable
#               logger that writes messages to both a pipeline log
#               file and the console.
#
#***************************************************************

import logging
from pathlib import Path


#***************************************************************
# Log File Configuration
#
# Defines the default log file location used by the AMH pipeline.
# The logs folder is created automatically if it does not exist.
#***************************************************************

LOG_FILE = "logs/pipeline.log"


#***************************************************************
#
#  Function:     get_logger
#
#  Description: Creates and returns a configured logger for the AMH
#               pipeline. The logger writes formatted INFO-level
#               messages to both a log file and the console. If the
#               logger already has handlers, the existing logger is
#               returned to avoid duplicate log entries.
#
#  Parameters:  name - Logger name used to identify the source of
#                      log messages.
#
#  Returns:     Logger - Configured Python logger instance.
#
#***************************************************************

def get_logger(name="amh_pipeline"):
    # Get or create a logger with the provided name.
    logger = logging.getLogger(name)

    # If handlers already exist, return the logger as-is.
    # This prevents duplicate file or console messages.
    if logger.handlers:
        return logger

    # Set the logger to record INFO-level messages and above.
    logger.setLevel(logging.INFO)

    # Create the logs folder if it does not already exist.
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Define a consistent format for all log messages.
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Configure the file handler so log messages are saved to disk.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # Configure the stream handler so log messages also appear in the console.
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    # Attach both handlers to the logger.
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger

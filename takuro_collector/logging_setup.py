from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import log_dir


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("takuro_collector")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    path = log_dir() / "collector.log"
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    setup_logging()
    return logging.getLogger("takuro_collector" + ("." + name if name else ""))

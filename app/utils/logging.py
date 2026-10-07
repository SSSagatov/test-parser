"""Structured audit logs omit configuration secrets and HTTP authorization."""

import sys

from loguru import logger

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    settings.log_folder.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    logger.add(
        settings.log_folder / "crawler.jsonl",
        serialize=True,
        rotation="50 MB",
        retention="30 days",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

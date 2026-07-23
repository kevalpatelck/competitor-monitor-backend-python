import sys
import io
import logging
from datetime import datetime, timezone
from pathlib import Path


class CustomFormatter(logging.Formatter):
    def format(self, record):
        now_utc = datetime.now(timezone.utc)
        timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
        level = record.levelname
        msg = record.getMessage()
        formatted = f"[{timestamp}] [{level}] {msg}"
        if record.exc_info:
            formatted += "\n" + self.formatException(record.exc_info)
        return formatted


def setup_logger(name: str = "competitor-monitor"):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = CustomFormatter()

    # Console (UTF-8 for Windows)
    utf8_stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    console_handler = logging.StreamHandler(utf8_stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File log — always on for debugging Auto-Fill / scrape issues
    logs_dir = Path(__file__).resolve().parents[2] / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(logs_dir / "api.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info(f"[LOGGER] File logging enabled → {logs_dir / 'api.log'}")
    return logger


logger = setup_logger()

import logging
import sys
from pathlib import Path
from datetime import datetime

def setup_logger(name: str, log_level=logging.INFO) -> logging.Logger:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # setup_logger runs at module import; a re-import must not stack duplicate handlers.
    if logger.handlers:
        return logger

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(log_dir / f"{name}_{timestamp}.log", encoding="utf-8")

    # The Windows console defaults to cp1252, which raises UnicodeEncodeError on
    # any non-latin-1 character and silently drops the log record. Reconfigure the
    # stream to UTF-8 with replacement so a stray character can never lose a log line.
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    ch = logging.StreamHandler(stream)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger

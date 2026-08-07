import logging
import os
from datetime import datetime

import config


def setup_logging() -> None:
    config.Paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Include microseconds + PID so concurrent or same-second runs never share a file.
    log_path = config.Paths.LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )

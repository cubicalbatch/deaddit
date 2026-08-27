"""Central logging configuration for Deaddit.

Consolidates the previously split logging setup (stdlib `basicConfig` in
`deaddit/__init__.py` plus loguru usage across loader/jobs/admin/websocket)
onto stdlib `logging` alone, configured once via `dictConfig`.

Handlers:
- stdout console handler: always on (gunicorn already logs access/errors to
  stdout; container deployments rely on this).
- RotatingFileHandler (10 MB x 5 backups): enabled unless ``DEADDIT_LOG_FILE``
  is set to an empty string. Defaults to ``instance/deaddit.log``. The Docker
  image sets ``DEADDIT_LOG_FILE=""`` for stdout-only logging.
"""

import logging
import logging.config
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def configure_logging() -> None:
    """Configure root logging. Safe to call multiple times."""
    level = os.environ.get("DEADDIT_LOG_LEVEL", "INFO").upper()
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "standard",
        }
    }

    file_target = os.environ.get("DEADDIT_LOG_FILE")
    if file_target is None:
        file_target = str(
            Path(__file__).resolve().parent.parent / "instance" / "deaddit.log"
        )
    if file_target:
        Path(file_target).parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": file_target,
            "maxBytes": MAX_BYTES,
            "backupCount": BACKUP_COUNT,
            "formatter": "standard",
        }

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"standard": {"format": LOG_FORMAT}},
            "handlers": handlers,
            "root": {"level": level, "handlers": list(handlers)},
        }
    )

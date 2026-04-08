from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import LOGS_DIR

LOG_FILE = LOGS_DIR / "application_log.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "server.log", encoding="utf-8"),
    ],
)

logger = logging.getLogger("job-search-ai")


def _read_log_entries() -> list[dict[str, Any]]:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _write_log_entries(entries: list[dict[str, Any]]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def log_event(
    event_type: str,
    portal: str = "",
    job_title: str = "",
    company: str = "",
    url: str = "",
    status: str = "",
    detail: str = "",
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "portal": portal,
        "job_title": job_title,
        "company": company,
        "url": url,
        "status": status,
        "detail": detail,
    }
    entries = _read_log_entries()
    entries.append(entry)
    _write_log_entries(entries)
    logger.info("%s | %s | %s @ %s | %s", event_type, status, job_title, company, detail)


def get_log_entries() -> list[dict[str, Any]]:
    return _read_log_entries()


def clear_logs() -> None:
    _write_log_entries([])

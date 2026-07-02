"""
Markdown run reporter — appends structured session logs to run_report.md.

Usage
-----
    with Reporter() as r:
        r.session_start("fetch-audio-features")
        r.attach_logger("spotify.audio_features.reccobeats")
        r.write("**Pending:** 3439")
        # ... do work ...
        r.write_summary({"Resolved": 2812, "Not found": 627})
        r.session_end()

Log messages from attached loggers are written as list items under a
"### Log" heading.  Explicit `r.write()` calls go directly into the
section body.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

REPORT_FILE = Path(__file__).parent.parent / "run_report.md"


class MarkdownLogHandler(logging.Handler):
    """Appends log records as Markdown list items to an open file handle."""

    def __init__(self, fh, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._fh = fh

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._fh.write(f"- `{record.levelname}` {msg}\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)


class Reporter:
    def __init__(self, path: Path = REPORT_FILE) -> None:
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._attached: list[tuple[logging.Logger, MarkdownLogHandler]] = []

    # ------------------------------------------------------------------
    # Session boundaries
    # ------------------------------------------------------------------

    def session_start(self, command: str) -> None:
        now = _now()
        self._fh.write(f"\n---\n\n## {now} — `{command}`\n\n")
        self._fh.flush()

    def session_end(self) -> None:
        self._fh.write(f"\n_Completed: {_now()}_\n")
        self._fh.flush()

    # ------------------------------------------------------------------
    # Body writes
    # ------------------------------------------------------------------

    def write(self, line: str = "") -> None:
        self._fh.write(line + "\n")
        self._fh.flush()

    def write_summary(self, fields: dict) -> None:
        """Write a ### Summary block with key/value pairs."""
        self._fh.write("\n### Summary\n\n")
        for key, value in fields.items():
            self._fh.write(f"- **{key}:** {value}\n")
        self._fh.flush()

    def begin_log_section(self) -> None:
        self._fh.write("\n### Log\n\n")
        self._fh.flush()

    # ------------------------------------------------------------------
    # Logger attachment
    # ------------------------------------------------------------------

    def attach_logger(
        self,
        logger_name: str,
        level: int = logging.INFO,
    ) -> None:
        """Forward log records from *logger_name* into the report file."""
        logger = logging.getLogger(logger_name)
        # Ensure the logger itself lets records through at this level,
        # regardless of the root logger's configuration.
        if logger.level == logging.NOTSET or logger.level > level:
            logger.setLevel(level)
        handler = MarkdownLogHandler(self._fh, level)
        handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
        logger.addHandler(handler)
        self._attached.append((logger, handler))

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        for logger, handler in self._attached:
            logger.removeHandler(handler)
            logger.setLevel(logging.NOTSET)  # restore to inherit from parent
        self._fh.close()

    def __enter__(self) -> "Reporter":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

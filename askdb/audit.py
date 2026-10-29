"""Append-only JSONL audit log: what was asked and run, never the data that came back."""

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("askdb")


@dataclass
class AuditRecord:
    """The complete set of fields that are ever logged. There is deliberately no field for rows."""

    question: str
    sql: str | None
    status: str  # "ok", or the kind of failure (guard, sql, timeout, llm, ...)
    row_count: int | None
    duration_s: float
    retries: int
    llm_calls: int
    input_tokens: int
    output_tokens: int


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = 5_000_000, backups: int = 3):
        self._path = path
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

    def record(self, entry: AuditRecord) -> None:
        """Append one line. A logging failure must never break answering a question."""
        line = json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **asdict(entry)},
            ensure_ascii=False,
        )
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed(len(line) + 1)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            log.warning("could not write audit log %s: %s", self._path, exc.strerror or exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        if not self._path.exists() or self._path.stat().st_size + incoming <= self._max_bytes:
            return
        for n in range(self._backups, 0, -1):
            src = self._path if n == 1 else self._path.with_name(f"{self._path.name}.{n - 1}")
            dst = self._path.with_name(f"{self._path.name}.{n}")
            if src.exists():
                src.replace(dst)  # the oldest backup is overwritten

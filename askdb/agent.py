"""Question pipeline: interpret (LLM) -> guard -> execute -> summarize, as a stream of events.

Only the `status == "ok"` path exists so far; other statuses end the stream with an error event.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from askdb.audit import AuditLog, AuditRecord
from askdb.executor import Executor, QueryError, QueryTimeout
from askdb.llm import BudgetedLLM, LLMClient, LLMError
from askdb.prompting import RESPONSE_SCHEMA, build_system, build_user
from askdb.sql_guard import GuardError
from askdb.summarizer import Summarizer


@dataclass
class Event:
    name: str
    data: dict[str, Any]


def _error(kind: str, message: str) -> Event:
    return Event("error", {"kind": kind, "message": message})


@dataclass
class _Trace:
    """What happened during one question, for the audit record."""

    status: str = "cancelled"  # replaced as soon as the question reaches an outcome
    sql: str | None = None
    row_count: int | None = None


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        executor: Executor,
        schema_info: dict[str, Any],
        audit: AuditLog | None = None,
    ):
        self._llm = llm
        self._executor = executor
        self._system = build_system(schema_info)
        self._summarizer = Summarizer(llm)
        self._audit = audit

    def ask(self, question: str) -> Iterator[Event]:
        trace = _Trace()
        started = time.monotonic()
        try:
            yield from self._run(question, trace)
        finally:  # also runs when the consumer stops early, which is logged as "cancelled"
            self._record(question, trace, time.monotonic() - started)

    def _record(self, question: str, trace: _Trace, duration_s: float) -> None:
        if self._audit is None:
            return
        # Only a BudgetedLLM keeps tallies; read them by type, never by attribute name.
        tally = self._llm if isinstance(self._llm, BudgetedLLM) else None
        self._audit.record(
            AuditRecord(
                question=question,
                sql=trace.sql,
                status=trace.status,
                row_count=trace.row_count,
                duration_s=round(duration_s, 3),
                retries=0,
                llm_calls=tally.calls if tally else 0,
                input_tokens=tally.input_tokens if tally else 0,
                output_tokens=tally.output_tokens if tally else 0,
            )
        )

    def _run(self, question: str, trace: _Trace) -> Iterator[Event]:
        yield Event("stage", {"stage": "understanding"})
        try:
            result = self._llm.complete_json(self._system, build_user(question), RESPONSE_SCHEMA)
        except LLMError as exc:
            trace.status = "llm"
            yield _error("llm", exc.message)
            return
        plan = result.data

        status = plan.get("status")
        if status != "ok":
            trace.status = "unanswerable" if status == "unanswerable" else "unsupported"
            message = plan.get("message") or "This question could not be answered."
            yield _error(trace.status, message)
            return

        trace.sql = plan["sql"]
        yield Event(
            "interpretation",
            {
                "sql": plan["sql"],
                "assumptions": plan["assumptions"],
                "filters": plan["filters"],
                "method_steps": plan["method_steps"],
                "confidence": plan["confidence"],
            },
        )

        yield Event("stage", {"stage": "running"})
        try:
            rows = self._executor.run(plan["sql"])
        except GuardError as exc:
            trace.status = "guard"
            yield _error("guard", exc.reason)
            return
        except QueryTimeout as exc:
            trace.status = "timeout"
            yield _error("timeout", str(exc))
            return
        except QueryError as exc:
            trace.status = "sql"
            yield _error("sql", exc.message)
            return

        trace.row_count = len(rows.rows)
        yield Event(
            "table",
            {
                "columns": rows.columns,
                "rows": [list(r) for r in rows.rows],
                "row_count": len(rows.rows),
                "truncated": rows.truncated,
            },
        )

        yield Event("stage", {"stage": "summarizing"})
        summary = self._summarizer.summarize(
            question, plan["sql"], rows.columns, rows.rows, rows.truncated
        )
        trace.status = "ok"
        yield Event(
            "summary", {"text": summary.text, "note": summary.note, "verified": summary.verified}
        )
        yield Event("done", {})

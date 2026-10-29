"""Question pipeline: interpret (LLM) -> guard -> execute, as a stream of events.

Only the `status == "ok"` path exists so far; other statuses end the stream with an error event.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from askdb.executor import Executor, QueryError, QueryTimeout
from askdb.llm import LLMClient, LLMError
from askdb.prompting import RESPONSE_SCHEMA, build_system, build_user
from askdb.sql_guard import GuardError


@dataclass
class Event:
    name: str
    data: dict[str, Any]


def _error(kind: str, message: str) -> Event:
    return Event("error", {"kind": kind, "message": message})


class Agent:
    def __init__(self, llm: LLMClient, executor: Executor, schema_info: dict[str, Any]):
        self._llm = llm
        self._executor = executor
        self._system = build_system(schema_info)

    def ask(self, question: str) -> Iterator[Event]:
        yield Event("stage", {"stage": "understanding"})
        try:
            result = self._llm.complete_json(self._system, build_user(question), RESPONSE_SCHEMA)
        except LLMError as exc:
            yield _error("llm", exc.message)
            return
        plan = result.data

        status = plan.get("status")
        if status != "ok":
            message = plan.get("message") or "This question could not be answered."
            kind = "unanswerable" if status == "unanswerable" else "unsupported"
            yield _error(kind, message)
            return

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
            yield _error("guard", exc.reason)
            return
        except QueryTimeout as exc:
            yield _error("timeout", str(exc))
            return
        except QueryError as exc:
            yield _error("sql", exc.message)
            return

        yield Event(
            "table",
            {
                "columns": rows.columns,
                "rows": [list(r) for r in rows.rows],
                "row_count": len(rows.rows),
                "truncated": rows.truncated,
            },
        )
        yield Event("done", {})

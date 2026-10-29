"""FastAPI app: startup health check and schema info."""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from askdb.agent import Agent, Event
from askdb.audit import AuditLog
from askdb.executor import Executor
from askdb.glossary import (
    GlossaryError,
    apply_excluded,
    excluded_set,
    load_glossary,
    validate_glossary,
)
from askdb.llm import AnthropicLLM, BudgetedLLM, LLMClient
from askdb.schema import introspect, merge_glossary
from askdb.settings import Settings, get_settings

log = logging.getLogger("askdb")


@dataclass
class AppState:
    settings: Settings
    issues: list[str] = field(default_factory=list)
    schema_info: dict[str, Any] | None = None
    executor: Executor | None = None
    llm: LLMClient | None = None
    audit: AuditLog | None = None


def build_state(settings: Settings) -> AppState:
    """Run the health check. Every problem found becomes an actionable message in `issues`.

    A missing API key does not stop schema loading, but is still reported.
    """
    state = AppState(settings)
    state.audit = AuditLog(settings.audit_log_path, settings.audit_max_bytes)
    if not settings.has_api_key:
        state.issues.append(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    db_path = settings.db_path
    if not db_path.is_file():
        state.issues.append(
            f"Database file not found at {db_path}. Set DB_PATH in .env to an existing SQLite file."
        )
        return state

    try:
        full = introspect(db_path, with_distincts=False)
    except sqlite3.Error as exc:
        state.issues.append(
            f"Cannot read {db_path} as a SQLite database ({exc}). Check DB_PATH and the file."
        )
        return state
    if not full.tables:
        state.issues.append(f"{db_path} has no tables, so there is nothing to ask about.")
        return state

    try:
        glossary = load_glossary(settings.glossary_path)
    except GlossaryError as exc:
        state.issues.extend(f"Glossary: {e}" for e in exc.errors)
        return state
    problems = validate_glossary(glossary, full, db_path)
    if problems:
        state.issues.extend(f"Glossary: {p}" for p in problems)
        return state

    visible = introspect(db_path, excluded=excluded_set(glossary))
    if not visible.tables:
        state.issues.append("Every table is listed under `excluded:` in the glossary.")
        return state
    state.schema_info = merge_glossary(visible, apply_excluded(glossary))
    state.executor = Executor.from_settings(
        settings,
        allowed_tables=frozenset(t.name for t in visible.tables),
        denied_columns=frozenset(e for e in glossary.excluded if "." in e),
    )
    if settings.has_api_key:
        state.llm = AnthropicLLM(settings.anthropic_api_key.get_secret_value(), settings.llm_model)
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = build_state(get_settings())
    for issue in state.issues:
        log.error("health check: %s", issue)
    app.state.askdb = state
    yield


app = FastAPI(title="AskDB", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, Any]:
    state: AppState = app.state.askdb
    return {"ok": not state.issues, "issues": state.issues}


@app.get("/api/schema-info")
def schema_info() -> dict[str, Any]:
    state: AppState = app.state.askdb
    if state.schema_info is None:
        raise HTTPException(status_code=503, detail={"issues": state.issues})
    return state.schema_info


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _sse(event: Event) -> str:
    payload = json.dumps(event.data, default=str)
    return "event: " + event.name + "\ndata: " + payload + "\n\n"


@app.post("/api/ask")
def ask(request: AskRequest) -> StreamingResponse:
    state: AppState = app.state.askdb
    if state.schema_info is None or state.executor is None:
        raise HTTPException(status_code=503, detail={"issues": state.issues})
    if state.llm is None:
        raise HTTPException(
            status_code=503,
            detail={"issues": [i for i in state.issues if "ANTHROPIC_API_KEY" in i]},
        )
    llm = BudgetedLLM(state.llm, state.settings.llm_max_calls_per_question)
    agent = Agent(llm, state.executor, state.schema_info, state.audit)

    def stream() -> Iterator[str]:
        for event in agent.ask(request.question):
            yield _sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

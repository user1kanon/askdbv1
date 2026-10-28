"""Startup health check: turns misconfiguration into actionable messages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from askdb.glossary import GlossaryError, load_glossary, validate_glossary
from askdb.schema import SchemaError, SchemaInfo, build_schema_info, introspect
from askdb.settings import Settings


class Problem(BaseModel):
    level: Literal["error", "warning"]
    message: str


class HealthReport(BaseModel):
    problems: list[Problem] = Field(default_factory=list)
    schema_info: SchemaInfo | None = Field(default=None, exclude=True)

    @property
    def ok(self) -> bool:
        return not any(p.level == "error" for p in self.problems)


def run_health_check(settings: Settings) -> HealthReport:
    """Check key, database and glossary; `schema_info` is set only if DB and glossary are usable."""
    report = HealthReport()

    def error(msg: str) -> None:
        report.problems.append(Problem(level="error", message=msg))

    if not settings.anthropic_api_key.strip():
        error("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your API key.")

    try:
        full = introspect(settings.db_path, distinct_cap=0)
    except SchemaError as exc:
        error(f"{exc}. Check DB_PATH in .env.")
        return report
    if not full.tables:
        error(f"The database at {settings.db_path} has no tables.")
        return report

    try:
        glossary = load_glossary(settings.glossary_path)
    except GlossaryError as exc:
        for msg in exc.errors:
            error(f"Glossary {settings.glossary_path}: {msg}")
        return report
    problems = validate_glossary(glossary, full, settings.db_path)
    if problems:
        for msg in problems:
            error(f"Glossary {settings.glossary_path}: {msg}")
        return report

    schema = build_schema_info(settings.db_path, glossary)
    if not schema.tables:
        error("Every table is excluded by the glossary, so the model would see no schema.")
        return report
    report.schema_info = schema
    return report

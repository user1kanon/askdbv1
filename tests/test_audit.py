import json
import logging

from askdb.agent import Agent
from askdb.audit import AuditLog, AuditRecord
from askdb.llm import StubLLM
from askdb.main import build_state
from askdb.settings import PROJECT_ROOT, Settings


def rec(question="q", sql="SELECT 1", **kw):
    base = {
        "question": question,
        "sql": sql,
        "status": "ok",
        "row_count": 1,
        "duration_s": 0.1,
        "retries": 0,
        "llm_calls": 2,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    return AuditRecord(**{**base, **kw})


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_appends_one_json_line_per_record(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"  # parent directory is created
    log = AuditLog(path)
    log.record(rec("first"))
    log.record(rec("second", sql=None, status="llm", row_count=None))
    first, second = lines(path)
    assert first["question"] == "first" and first["status"] == "ok"
    assert set(first) == {
        "timestamp",
        "question",
        "sql",
        "status",
        "row_count",
        "duration_s",
        "retries",
        "llm_calls",
        "input_tokens",
        "output_tokens",
    }
    assert second["sql"] is None and second["row_count"] is None


def test_record_type_has_no_field_for_result_data():
    fields = AuditRecord.__dataclass_fields__
    assert not any(name in fields for name in ("rows", "result", "table", "summary"))


def test_end_to_end_log_contains_no_row_values(tmp_path, sample_db, sample_glossary):
    path = tmp_path / "e2e.jsonl"
    state = build_state(Settings(_env_file=None, db_path=sample_db, glossary_path=sample_glossary))
    plan = {
        "status": "ok",
        "sql": "SELECT company_name FROM customers LIMIT 5",
        "assumptions": [],
        "filters": [],
        "method_steps": ["s"],
        "confidence": "high",
        "message": "",
    }
    stub = StubLLM([plan, {"summary": "Five customers."}])
    events = list(Agent(stub, state.executor, state.schema_info, AuditLog(path)).ask("names?"))
    names = [r[0] for r in next(e for e in events if e.name == "table").data["rows"]]
    assert len(names) == 5
    text = path.read_text(encoding="utf-8")
    assert all(n not in text for n in names)
    # Regression: a client that is not a BudgetedLLM (here StubLLM, whose `.calls` holds whole
    # prompts) must log zero tallies, never its own attributes.
    entry = lines(path)[0]
    assert (entry["llm_calls"], entry["input_tokens"], entry["output_tokens"]) == (0, 0, 0)
    assert "Five customers" not in text  # the summary is derived from rows, so it is not logged


def test_unicode_is_kept_readable(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).record(rec("café revenue?"))
    assert "café" in path.read_text(encoding="utf-8")


def test_rotates_by_size_and_keeps_limited_backups(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=600, backups=2)
    for i in range(30):
        log.record(rec(f"question {i}"))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "audit.jsonl",
        "audit.jsonl.1",
        "audit.jsonl.2",
    ]
    assert path.stat().st_size <= 600
    newest = lines(path)[-1]["question"]
    assert newest == "question 29"
    assert lines(tmp_path / "audit.jsonl.1")[-1]["question"] != newest
    for p in tmp_path.iterdir():  # every file is whole lines of valid JSON
        assert all(json.loads(x) for x in p.read_text(encoding="utf-8").splitlines())


def test_write_failure_never_raises_or_leaks_the_question(tmp_path, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    with caplog.at_level(logging.WARNING, logger="askdb"):
        AuditLog(blocked / "audit.jsonl").record(rec("secret question"))
    assert "could not write audit log" in caplog.text
    assert "secret question" not in caplog.text


def test_log_location_is_under_the_project_and_git_ignored():
    settings = Settings(_env_file=None, audit_log_path="logs/audit.jsonl")
    assert settings.audit_log_path == PROJECT_ROOT / "logs" / "audit.jsonl"
    ignored = (PROJECT_ROOT / ".gitignore").read_text()
    assert "logs/" in ignored and "*.jsonl" in ignored

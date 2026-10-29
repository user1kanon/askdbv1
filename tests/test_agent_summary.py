import json

import pytest

from askdb.agent import Agent
from askdb.audit import AuditLog
from askdb.llm import BudgetedLLM, LLMError, StubLLM
from askdb.main import build_state
from askdb.settings import Settings


def ok(sql, **kw):
    plan = {
        "status": "ok",
        "sql": sql,
        "assumptions": [],
        "filters": [],
        "method_steps": ["s"],
        "confidence": "high",
        "message": "",
    }
    plan.update(kw)
    return plan


def summ(text):
    return {"summary": text}


@pytest.fixture
def state(sample_db, sample_glossary):
    return build_state(Settings(_env_file=None, db_path=sample_db, glossary_path=sample_glossary))


def run(state, *responses, audit=None, llm=None):
    stub = StubLLM(list(responses))
    agent = Agent(llm(stub) if llm else stub, state.executor, state.schema_info, audit)
    return list(agent.ask("how many plans?")), stub


def names(events):
    return [e.name for e in events]


def audit_lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_summary_event_follows_the_table(state):
    events, _ = run(state, ok("SELECT COUNT(*) AS n FROM plans"), summ("There are 4 plans."))
    summary = next(e for e in events if e.name == "summary")
    assert summary.data == {"text": "There are 4 plans.", "note": None, "verified": True}
    order = names(events)
    assert (
        order.index("table") < order.index("stage", order.index("table")) < order.index("summary")
    )
    assert events[order.index("summary") - 1].data == {"stage": "summarizing"}


def test_hallucinated_figure_is_regenerated_once(state):
    events, stub = run(
        state,
        ok("SELECT COUNT(*) AS n FROM plans"),
        summ("There are 9 plans."),
        summ("There are 4 plans."),
    )
    summary = next(e for e in events if e.name == "summary")
    assert summary.data["text"] == "There are 4 plans." and summary.data["verified"] is True
    assert len(stub.calls) == 3
    assert "9" in stub.calls[2].user  # the retry names the unsupported figure


def test_second_failure_falls_back_to_templated_summary(state):
    events, stub = run(
        state,
        ok("SELECT COUNT(*) AS n FROM plans"),
        summ("There are 9 plans."),
        summ("Actually 12 plans."),
    )
    summary = next(e for e in events if e.name == "summary")
    assert summary.data["verified"] is False
    assert summary.data["text"] == "The result is 4 (n)."
    assert "could not be checked" in summary.data["note"]
    assert len(stub.calls) == 3  # never a third attempt
    assert names(events)[-1] == "done"


def test_summary_llm_failure_degrades_to_templated_summary(state):
    events, _ = run(state, ok("SELECT plan_name FROM plans"), LLMError("down"))
    summary = next(e for e in events if e.name == "summary")
    assert summary.data["text"].startswith("The query returned 4 rows")
    assert "unavailable" in summary.data["note"]
    assert names(events)[-1] == "done"


def test_zero_rows_skip_the_summary_call(state):
    events, stub = run(state, ok("SELECT * FROM plans WHERE plan_id < 0"))
    summary = next(e for e in events if e.name == "summary")
    assert summary.data["text"] == "The query returned no rows."
    assert len(stub.calls) == 1


def test_audit_record_on_success(state, tmp_path):
    path = tmp_path / "a.jsonl"
    run(
        state,
        ok("SELECT plan_name FROM plans"),
        summ("Four plans."),
        audit=AuditLog(path),
        llm=lambda stub: BudgetedLLM(stub, 6),
    )
    (rec,) = audit_lines(path)
    assert rec["question"] == "how many plans?"
    assert rec["sql"] == "SELECT plan_name FROM plans"
    assert rec["status"] == "ok" and rec["row_count"] == 4 and rec["retries"] == 0
    assert rec["llm_calls"] == 2 and rec["input_tokens"] == 2 and rec["output_tokens"] == 2
    assert rec["duration_s"] >= 0 and rec["timestamp"].endswith("+00:00")


@pytest.mark.parametrize(
    ("responses", "status"),
    [
        ([ok("DROP TABLE plans")], "guard"),
        ([ok("SELECT nope FROM plans")], "sql"),
        ([ok("", status="unanswerable", message="no")], "unanswerable"),
        ([ok("", status="clarify", message="which?")], "unsupported"),
        ([LLMError("down")], "llm"),
    ],
)
def test_audit_records_failures(state, tmp_path, responses, status):
    path = tmp_path / "a.jsonl"
    run(state, *responses, audit=AuditLog(path))
    assert audit_lines(path)[0]["status"] == status


def test_abandoned_stream_is_audited_as_cancelled(state, tmp_path):
    path = tmp_path / "a.jsonl"
    stream = Agent(StubLLM([ok("SELECT 1")]), state.executor, state.schema_info, AuditLog(path))
    events = stream.ask("q")
    next(events)
    events.close()
    assert audit_lines(path)[0]["status"] == "cancelled"

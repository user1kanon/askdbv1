import json

import pytest
from fastapi.testclient import TestClient

from askdb import main
from askdb.llm import LLMError, StubLLM
from askdb.settings import Settings


def ok(sql, **kw):
    plan = {
        "status": "ok",
        "sql": sql,
        "assumptions": [],
        "filters": [],
        "method_steps": ["step"],
        "confidence": "high",
        "message": "",
    }
    plan.update(kw)
    return plan


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = chunk.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture
def api(monkeypatch, sample_db, sample_glossary):
    settings = Settings(
        _env_file=None,
        anthropic_api_key="sk-test",
        db_path=sample_db,
        glossary_path=sample_glossary,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as client:
        stub = StubLLM()
        main.app.state.askdb.llm = stub
        yield client, stub


def ask(client, question="how many plans?"):
    response = client.post("/api/ask", json={"question": question})
    return response, parse_sse(response.text) if response.status_code == 200 else None


def test_streams_stages_then_table(api):
    client, stub = api
    stub.responses.extend(
        [
            ok("SELECT plan_name FROM plans ORDER BY monthly_price"),
            {"summary": "There are 4 plans, from Starter to Enterprise."},
        ]
    )
    response, events = ask(client)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [name for name, _ in events] == [
        "stage",
        "interpretation",
        "stage",
        "table",
        "stage",
        "summary",
        "done",
    ]
    assert events[0][1] == {"stage": "understanding"}
    assert events[2][1] == {"stage": "running"}
    table = events[3][1]
    assert table["columns"] == ["plan_name"]
    assert table["rows"] == [["Starter"], ["Professional"], ["Business"], ["Enterprise"]]
    assert events[4][1] == {"stage": "summarizing"}
    assert events[5] == (
        "summary",
        {"text": "There are 4 plans, from Starter to Enterprise.", "note": None, "verified": True},
    )


def test_non_select_sql_yields_guard_error_event(api):
    client, stub = api
    stub.responses.append(ok("DELETE FROM customers"))
    _, events = ask(client)
    assert events[-1][0] == "error" and events[-1][1]["kind"] == "guard"
    assert "table" not in [name for name, _ in events]


def test_llm_error_event(api):
    client, stub = api
    stub.responses.append(LLMError("The AI service is busy."))
    _, events = ask(client)
    assert events[-1] == ("error", {"kind": "llm", "message": "The AI service is busy."})


def test_per_question_call_cap_applies(api):
    client, stub = api
    main.app.state.askdb.settings.llm_max_calls_per_question = 1
    stub.responses.extend([ok("SELECT 1"), {"summary": "x"}, ok("SELECT 2")])
    ask(client)
    assert len(stub.calls) == 1


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "x" * 2001}])
def test_invalid_request_is_422(api, body):
    client, _ = api
    assert client.post("/api/ask", json=body).status_code == 422


def test_missing_api_key_is_503_with_actionable_message(monkeypatch, sample_db, sample_glossary):
    settings = Settings(_env_file=None, db_path=sample_db, glossary_path=sample_glossary)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as client:
        response = client.post("/api/ask", json={"question": "hi"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]["issues"][0]


def test_unusable_database_is_503(monkeypatch, tmp_path, sample_glossary):
    settings = Settings(
        _env_file=None,
        anthropic_api_key="sk-test",
        db_path=tmp_path / "gone.db",
        glossary_path=sample_glossary,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as client:
        response = client.post("/api/ask", json={"question": "hi"})
    assert response.status_code == 503
    assert any("Database file not found" in i for i in response.json()["detail"]["issues"])

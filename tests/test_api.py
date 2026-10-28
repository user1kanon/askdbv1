from fastapi.testclient import TestClient

from askdb.main import create_app


def client(settings):
    return TestClient(create_app(settings))


def test_health_ok_and_schema_info_returns_merged_schema(make_settings):
    with client(make_settings()) as c:
        health = c.get("/api/health").json()
        assert health == {"problems": []}

        resp = c.get("/api/schema-info")
        assert resp.status_code == 200
        body = resp.json()
        customers = next(t for t in body["tables"] if t["name"] == "customers")
        assert customers["row_count"] == 200
        assert customers["description"].startswith("All registered customer companies")
        industry = next(col for col in customers["columns"] if col["name"] == "industry")
        assert "Technology" in industry["distinct_values"]
        assert body["description"].startswith("CloudMetrics")


def test_missing_api_key_is_reported_but_schema_still_served(make_settings):
    with client(make_settings(anthropic_api_key="")) as c:
        problems = c.get("/api/health").json()["problems"]
        assert [p["level"] for p in problems] == ["error"]
        assert "ANTHROPIC_API_KEY" in problems[0]["message"]
        assert ".env.example" in problems[0]["message"]
        assert c.get("/api/schema-info").status_code == 200


def test_unreadable_database_gives_actionable_503(make_settings, tmp_path):
    with client(make_settings(db_path=tmp_path / "gone.db")) as c:
        resp = c.get("/api/schema-info")
        assert resp.status_code == 503
        messages = [p["message"] for p in resp.json()["detail"]["problems"]]
        assert any("not found" in m and "DB_PATH" in m for m in messages)


def test_empty_database_is_reported(make_settings, tmp_path):
    import sqlite3

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with client(make_settings(db_path=empty)) as c:
        assert c.get("/api/schema-info").status_code == 503
        messages = [p["message"] for p in c.get("/api/health").json()["problems"]]
        assert any("no tables" in m for m in messages)


def test_bad_glossary_lists_every_error_and_blocks_schema(make_settings, tmp_path):
    g = tmp_path / "g.yaml"
    g.write_text("tables:\n  - name: nope\nexcluded: [ghost]\n")
    with client(make_settings(glossary_path=g)) as c:
        assert c.get("/api/schema-info").status_code == 503
        messages = [p["message"] for p in c.get("/api/health").json()["problems"]]
        assert sum("Glossary" in m for m in messages) == 2


def test_missing_glossary_is_reported(make_settings, tmp_path):
    with client(make_settings(glossary_path=tmp_path / "none.yaml")) as c:
        assert c.get("/api/schema-info").status_code == 503


def test_excluding_every_table_is_reported(make_settings, tmp_path):
    g = tmp_path / "g.yaml"
    g.write_text("excluded: [customers, plans, subscriptions, invoices, support_tickets]\n")
    with client(make_settings(glossary_path=g)) as c:
        assert c.get("/api/schema-info").status_code == 503
        messages = [p["message"] for p in c.get("/api/health").json()["problems"]]
        assert any("Every table is excluded" in m for m in messages)


def test_api_key_never_appears_in_responses(make_settings):
    with client(make_settings(anthropic_api_key="sk-super-secret")) as c:
        assert "sk-super-secret" not in c.get("/api/health").text
        assert "sk-super-secret" not in c.get("/api/schema-info").text

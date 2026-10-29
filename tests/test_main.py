import sqlite3

import pytest
from fastapi.testclient import TestClient

from askdb import main
from askdb.settings import Settings


def make_settings(db, glossary, key="sk-test"):
    return Settings(_env_file=None, anthropic_api_key=key, db_path=db, glossary_path=glossary)


def client_for(monkeypatch, settings):
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return TestClient(main.app)


def test_healthy_sample(monkeypatch, sample_db, sample_glossary):
    with client_for(monkeypatch, make_settings(sample_db, sample_glossary)) as client:
        assert client.get("/api/health").json() == {"ok": True, "issues": []}
        body = client.get("/api/schema-info").json()
    assert {t["name"] for t in body["tables"]} == {
        "customers",
        "plans",
        "subscriptions",
        "invoices",
        "support_tickets",
    }
    subs = next(t for t in body["tables"] if t["name"] == "subscriptions")
    assert subs["row_count"] == 282
    assert "MRR" in body["description"]


def test_missing_key_is_reported_but_schema_still_served(monkeypatch, sample_db, sample_glossary):
    settings = make_settings(sample_db, sample_glossary, key=None)
    with client_for(monkeypatch, settings) as client:
        issues = client.get("/api/health").json()["issues"]
        assert client.get("/api/schema-info").status_code == 200
    assert issues == ["ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."]


def test_blank_key_counts_as_missing(sample_db, sample_glossary):
    assert not make_settings(sample_db, sample_glossary, key="  ").has_api_key


def test_missing_db(monkeypatch, tmp_path, sample_glossary):
    settings = make_settings(tmp_path / "gone.db", sample_glossary)
    with client_for(monkeypatch, settings) as client:
        issues = client.get("/api/health").json()["issues"]
        resp = client.get("/api/schema-info")
    assert any("Database file not found" in i and "DB_PATH" in i for i in issues)
    assert resp.status_code == 503
    assert resp.json()["detail"]["issues"] == issues


def test_unreadable_db(monkeypatch, tmp_path, sample_glossary):
    bad = tmp_path / "not_sqlite.db"
    bad.write_text("this is not a database" * 50)
    with client_for(monkeypatch, make_settings(bad, sample_glossary)) as client:
        issues = client.get("/api/health").json()["issues"]
    assert any("as a SQLite database" in i for i in issues)


def test_empty_schema(monkeypatch, tmp_path, sample_glossary):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with client_for(monkeypatch, make_settings(empty, sample_glossary)) as client:
        issues = client.get("/api/health").json()["issues"]
        assert client.get("/api/schema-info").status_code == 503
    assert any("no tables" in i for i in issues)


def test_bad_glossary_file(monkeypatch, sample_db, tmp_path):
    glossary = tmp_path / "g.yaml"
    glossary.write_text("tables:\n  - name: dragons\n")
    with client_for(monkeypatch, make_settings(sample_db, glossary)) as client:
        issues = client.get("/api/health").json()["issues"]
        assert client.get("/api/schema-info").status_code == 503
    assert "Glossary: tables: unknown table 'dragons'" in issues


def test_missing_glossary_file(monkeypatch, sample_db, tmp_path):
    with client_for(monkeypatch, make_settings(sample_db, tmp_path / "none.yaml")) as client:
        issues = client.get("/api/health").json()["issues"]
    assert any(i.startswith("Glossary: Cannot read glossary file") for i in issues)


def test_everything_excluded(monkeypatch, tiny_db, tmp_path):
    glossary = tmp_path / "g.yaml"
    glossary.write_text("excluded: [owners, pets]\n")
    with client_for(monkeypatch, make_settings(tiny_db, glossary)) as client:
        issues = client.get("/api/health").json()["issues"]
    assert any("Every table is listed under `excluded:`" in i for i in issues)


def test_relative_paths_anchor_to_project_root():
    settings = Settings(_env_file=None, db_path="data/x.db")
    assert settings.db_path.is_absolute()
    assert settings.db_path.parts[-2:] == ("data", "x.db")


def test_env_vars_are_read(monkeypatch):
    monkeypatch.setenv("ROW_CAP", "123")
    assert Settings(_env_file=None).row_cap == 123
    monkeypatch.setenv("ROW_CAP", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)

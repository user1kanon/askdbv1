from pathlib import Path

from askdb.settings import Settings


def test_defaults(monkeypatch):
    for var in ("DB_PATH", "GLOSSARY_PATH", "ROW_CAP", "QUERY_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.row_cap == 10_000
    assert s.query_timeout_s == 10.0
    assert s.max_concurrent_queries == 2
    assert s.db_path == Path("data/cloudmetrics.db")
    assert s.anthropic_api_key == ""


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("ROW_CAP", "50")
    monkeypatch.setenv("DB_PATH", "elsewhere.db")
    s = Settings(_env_file=None)
    assert s.row_cap == 50
    assert s.db_path == Path("elsewhere.db")

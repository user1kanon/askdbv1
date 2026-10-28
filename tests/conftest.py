import sqlite3
from pathlib import Path

import pytest

from askdb.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def sample_db() -> Path:
    return ROOT / "data" / "cloudmetrics.db"


@pytest.fixture
def sample_glossary() -> Path:
    return ROOT / "data" / "cloudmetrics.yaml"


@pytest.fixture
def make_settings(sample_db, sample_glossary):
    def _make(**overrides) -> Settings:
        values = {
            "anthropic_api_key": "sk-test",
            "db_path": sample_db,
            "glossary_path": sample_glossary,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    """Small DB with an index, a many-valued text column and a secret column."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, ssn TEXT);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent,
                            note TEXT);
        CREATE TABLE secrets (id INTEGER PRIMARY KEY, token TEXT);
        CREATE UNIQUE INDEX idx_parent_name ON parent(name);
        CREATE INDEX idx_parent_ssn ON parent(ssn);
        """
    )
    conn.executemany(
        "INSERT INTO parent(name, kind, ssn) VALUES (?, ?, ?)",
        [(f"p{i}", "a" if i % 2 else "b", f"ssn{i}") for i in range(120)],
    )
    conn.execute("INSERT INTO child(parent_id, note) VALUES (1, 'x')")
    conn.execute("INSERT INTO secrets(token) VALUES ('s3cret')")
    conn.commit()
    conn.close()
    return path

import sqlite3
from pathlib import Path

import pytest

from askdb.settings import PROJECT_ROOT

SAMPLE_DB = PROJECT_ROOT / "data" / "cloudmetrics.db"
SAMPLE_GLOSSARY = PROJECT_ROOT / "data" / "cloudmetrics.yaml"


@pytest.fixture
def sample_db() -> Path:
    return SAMPLE_DB


@pytest.fixture
def sample_glossary() -> Path:
    return SAMPLE_GLOSSARY


@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    """A small DB with a FK, an index, a low-cardinality column and a high-cardinality one."""
    path = tmp_path / "tiny.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE pets (
            id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES owners(id),
            kind TEXT,
            nickname TEXT,
            born DATE
        );
        CREATE INDEX idx_pets_owner ON pets(owner_id);
        INSERT INTO owners VALUES (1, 'Ann'), (2, 'Bo');
        """
    )
    for i in range(60):
        conn.execute(
            "INSERT INTO pets VALUES (?, ?, ?, ?, ?)",
            (i, 1 + i % 2, "cat" if i % 2 else "dog", f"pet{i}", "2020-01-01"),
        )
    conn.commit()
    conn.close()
    return path

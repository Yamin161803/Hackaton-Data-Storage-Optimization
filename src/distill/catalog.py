import sqlite3
from pathlib import Path

from .config import DEFAULT_CATALOG_PATH

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS slides (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    slide_index INTEGER NOT NULL,
    text_content TEXT,
    text_hash TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    frame_index INTEGER NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    phash TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text_content TEXT,
    text_hash TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS duplicates (
    id INTEGER PRIMARY KEY,
    left_entity_type TEXT NOT NULL,
    left_entity_id INTEGER NOT NULL,
    right_entity_type TEXT NOT NULL,
    right_entity_id INTEGER NOT NULL,
    similarity REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    entity_type UNINDEXED,
    entity_id UNINDEXED,
    content
);
"""


def init_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> Path:
    catalog_path = Path(path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(catalog_path) as conn:
        conn.executescript(CATALOG_SCHEMA)
        conn.commit()
    return catalog_path

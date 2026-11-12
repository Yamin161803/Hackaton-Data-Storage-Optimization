import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from distill.cli import app


def test_cli_help_and_catalog_init(tmp_path: Path) -> None:
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Usage:" in help_result.output
    assert "init-catalog" in help_result.output

    db_path = tmp_path / "catalog.sqlite3"
    init_result = runner.invoke(app, ["init-catalog", "--path", str(db_path)])
    assert init_result.exit_code == 0
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"files", "slides", "frames", "segments", "duplicates"}.issubset(tables)

        fts_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='search_index'")
        }
        assert "search_index" in fts_tables

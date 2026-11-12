import typer

from .catalog import init_catalog

app = typer.Typer(help="distill command-line interface")


@app.callback()
def callback() -> None:
    """distill root command group."""


@app.command("init-catalog")
def init_catalog_command(path: str = typer.Option("out/catalog.sqlite3", help="SQLite catalog path")) -> None:
    catalog_path = init_catalog(path)
    typer.echo(f"Initialized catalog at {catalog_path}")


def main() -> None:
    app()

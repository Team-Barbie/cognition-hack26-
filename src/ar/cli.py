"""Command-line entry point for autoresearch."""

from __future__ import annotations

from pathlib import Path

import typer

from ar.db import initialize, read_status

app = typer.Typer(no_args_is_help=True, help="Local research with explicit contracts.")

PROJECT_DIRECTORIES = (
    "input/papers",
    "input/datasets",
    "input/notes",
    "policies",
    "skills",
    ".research/blobs",
    ".research/manifests",
    ".research/workspaces",
    "output/deliverables",
)


def _database(root: Path) -> Path:
    return root / ".research" / "state.sqlite"


def _not_ready(command: str) -> None:
    typer.echo(f"ar {command} is reserved for the next build wave.", err=True)
    raise typer.Exit(code=2)


@app.command("init")
def init_project(
    path: Path = typer.Argument(Path.cwd(), help="Directory to initialize."),
) -> None:
    """Create an autoresearch project and its SQLite state database."""

    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative in PROJECT_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    result = initialize(_database(root), root)
    action = "Initialized" if result.created else "Already initialized"
    typer.echo(f"{action} {result.project_id} at {root}")


@app.command()
def status(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
) -> None:
    """Show the persisted status of an autoresearch project."""

    try:
        summary = read_status(_database(path.resolve()))
    except FileNotFoundError:
        typer.echo("Not an autoresearch project. Run 'ar init' first.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Project: {summary.project_id}")
    typer.echo(f"Runs: {summary.runs}")
    typer.echo(f"Tasks: {summary.tasks}")


@app.command("run")
def run_project() -> None:
    """Run the research loop (implemented in Wave 1)."""

    _not_ready("run")


@app.command()
def inspect() -> None:
    """Inspect a claim or task (implemented in Wave 2)."""

    _not_ready("inspect")


@app.command("export")
def export_project() -> None:
    """Export reviewed deliverables (implemented with the Quality track)."""

    _not_ready("export")


if __name__ == "__main__":  # pragma: no cover
    app()

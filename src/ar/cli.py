"""Command-line entry point for autoresearch."""

from __future__ import annotations

from pathlib import Path

import typer

from ar.agents.fake import FakeAgent
from ar.controller import run_project as execute_project
from ar.db import init_project as initialize_project, read_status

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
    source: Path | None = typer.Option(None, "--from", help="Copy inputs from a fixture project."),
) -> None:
    """Create an autoresearch project and its SQLite state database."""

    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative in PROJECT_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    existed = _database(root).is_file()
    connection = initialize_project(root, source)
    try:
        project_id = connection.execute("SELECT id FROM project LIMIT 1").fetchone()["id"]
    finally:
        connection.close()
    action = "Already initialized" if existed else "Initialized"
    typer.echo(f"{action} {project_id} at {root}")


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
    if summary.latest_run_status:
        typer.echo(f"Latest run: {summary.latest_run_status}")
    for state, count in summary.task_states.items():
        typer.echo(f"  {state}: {count}")


@app.command("run")
def run_project(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
    fake: bool = typer.Option(False, "--fake", help="Use the deterministic fixture agent."),
    max_model_calls: int = typer.Option(20, min=0, help="Stop before dispatching past this budget."),
) -> None:
    """Run the fixed planner, reader, writer, and reviewer loop."""

    root = path.resolve()
    if not _database(root).is_file():
        connection = initialize_project(root)
        connection.close()
    if not fake:
        typer.echo("Only --fake is connected in this build.", err=True)
        raise typer.Exit(code=2)
    try:
        outcome = execute_project(root, FakeAgent(), max_model_calls=max_model_calls)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"Run failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Status: {outcome.value}")
    report = root / "output" / "report.md"
    if report.is_file():
        typer.echo(f"Report: {report}")


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

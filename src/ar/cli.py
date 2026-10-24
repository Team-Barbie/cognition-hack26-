"""Command-line entry point for autoresearch."""

from __future__ import annotations

from pathlib import Path

import typer

from ar.adapter import PROVIDERS, AnthropicClient, LiveAgent, load_env, make_client
from ar.agents.fake import FakeAgent
from ar.broker import Broker
from ar.controller import export_project as export_committed_project
from ar.controller import run_project as execute_project
from ar.db import connect, init_project as initialize_project, inspect_task, read_status, request_pause
from ar.evidence import inspect_claim

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


def _live_agent(
    provider: str, model: str | None, effort: str, base_url: str | None, root: Path | None = None
) -> LiveAgent:
    load_env(*([root / ".env"] if root else []), Path.cwd() / ".env")
    if provider == "anthropic":
        return LiveAgent(AnthropicClient(model=model or "claude-opus-5-5", effort=effort))
    return LiveAgent(make_client(provider, model, effort, base_url))


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
    if summary.stale_tasks:
        typer.echo(f"Stale tasks: {summary.stale_tasks}")
    for state, count in summary.task_states.items():
        typer.echo(f"  {state}: {count}")


@app.command("run")
def run_project(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
    fake: bool = typer.Option(False, "--fake", help="Use the deterministic fixture agent."),
    provider: str = typer.Option("anthropic", "--provider", help="Research model provider: anthropic or openai."),
    model: str | None = typer.Option(None, "--model", help="Model id for a live run."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible endpoint (openai provider)."),
    effort: str = typer.Option("medium", "--effort", help="Live model effort."),
    max_tasks: int = typer.Option(12, min=4, help="Maximum research tasks dispatched."),
    max_model_calls: int = typer.Option(20, min=0, help="Maximum model calls for the research run."),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    """Run the research agents with the configured model provider."""

    root = path.resolve()
    if not _database(root).is_file():
        connection = initialize_project(root)
        connection.close()
    if provider not in PROVIDERS:
        typer.echo(f"--provider must be one of: {', '.join(PROVIDERS)}.", err=True)
        raise typer.Exit(code=2)
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        typer.echo("--effort must be low, medium, high, xhigh, or max.", err=True)
        raise typer.Exit(code=2)
    agent = FakeAgent() if fake else _live_agent(provider, model, effort, base_url, root)
    try:
        outcome = execute_project(
            root,
            agent,
            max_tasks=max_tasks,
            max_model_calls=max_model_calls,
            idempotency_key=idempotency_key,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"Run failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Status: {outcome.value}")
    report = root / "output" / "report.md"
    if report.is_file():
        typer.echo(f"Report: {report}")


@app.command("literature")
def literature(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
    question: str | None = typer.Option(None, help="Create and run one review; otherwise read input/literature.yaml."),
    review_id: str = typer.Option("literature", "--id", help="Independent review instance id."),
    reviews: list[str] | None = typer.Option(None, "--review", help="Run selected configured ids; repeat for multiple reviews."),
    backends: list[str] | None = typer.Option(None, "--backend", help="crossref, europe_pmc, or brave; repeat to combine."),
    provider: str = typer.Option("anthropic", help="Literature model provider: anthropic or openai."),
    model: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    max_model_calls: int = typer.Option(24, min=0),
    max_searches: int = typer.Option(8, min=1),
    max_documents: int = typer.Option(8, min=1),
) -> None:
    """Search, read and synthesize independently scoped literature-review agents."""
    from ar.literature import LiteratureReviewAgent, ReviewSpec, load_reviews
    from ar.literature_client import review_agent

    root = path.resolve()
    try:
        if provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
        if not _database(root).is_file():
            initialize_project(root).close()
        specs = ([ReviewSpec(id=review_id, question=question, backends=backends or ["crossref", "europe_pmc"],
                             max_model_calls=max_model_calls, max_searches=max_searches, max_documents=max_documents)]
                 if question else load_reviews(root))
        if reviews:
            missing = set(reviews) - {spec.id for spec in specs}
            if missing:
                raise ValueError(f"unknown review ids: {', '.join(sorted(missing))}")
            specs = [spec for spec in specs if spec.id in reviews]
        if not specs:
            raise ValueError("provide --question or configure reviews in input/literature.yaml")
        runner = LiteratureReviewAgent(root, review_agent(root, provider=provider, model=model, base_url=base_url))
        for spec in specs:
            result = runner.run(spec)
            typer.echo(f"{result.id}: {result.status}; {len(result.claim_ids)} accepted claims; {result.searches} searches; {result.model_calls} model calls")
            typer.echo(f"Report: {root / result.report_path}")
            typer.echo(f"Stopped: {result.stop_reason}")
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"Literature review failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def inspect(
    kind: str = typer.Argument(help="claim, task, or reads"),
    identifier: str = typer.Argument(help="Claim or task id."),
    project: Path = typer.Option(Path.cwd(), "--project", help="Project directory."),
) -> None:
    """Inspect committed claims, tasks, or read receipts."""

    database = _database(project.resolve())
    if not database.is_file():
        typer.echo("Not an autoresearch project.", err=True)
        raise typer.Exit(code=1)
    connection = connect(database)
    try:
        if kind == "claim":
            typer.echo(inspect_claim(connection, identifier))
        elif kind == "task":
            typer.echo(inspect_task(database, identifier))
        elif kind == "reads":
            attempt = connection.execute(
                """SELECT id FROM attempts WHERE task_id=?
                   ORDER BY number DESC LIMIT 1""",
                (identifier,),
            ).fetchone()
            if attempt is None:
                typer.echo(f"No reads for task: {identifier}")
            else:
                receipts = Broker(connection, project.resolve()).receipts_for(
                    identifier, attempt["id"]
                )
                if not receipts:
                    typer.echo(f"No reads for task: {identifier}")
                for receipt in receipts:
                    typer.echo(
                        f"{receipt.id}: {receipt.source_ref.source_id} "
                        f"r{receipt.source_ref.revision} lines "
                        f"{receipt.selector.start}-{receipt.selector.end} "
                        f"attempt={receipt.attempt_id}"
                    )
        else:
            typer.echo("Kind must be claim, task, or reads.", err=True)
            raise typer.Exit(code=2)
    finally:
        connection.close()


@app.command("export")
def export_project(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
) -> None:
    """Re-export the newest committed deliverable."""

    try:
        paths = export_committed_project(path.resolve())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for exported in paths:
        typer.echo(str(exported))


@app.command()
def pause(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
) -> None:
    """Request that the active run stop before its next task."""

    try:
        run_id = request_pause(_database(path.resolve()))
    except (FileNotFoundError, RuntimeError) as exc:
        typer.echo(f"Pause failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Pause requested for {run_id}")


@app.command()
def resume(
    path: Path = typer.Argument(Path.cwd(), help="Project directory."),
    fake: bool = typer.Option(False, "--fake", help="Use the deterministic fixture agent."),
    provider: str = typer.Option("anthropic", "--provider", help="Research model provider: anthropic or openai."),
    model: str | None = typer.Option(None, "--model", help="Model id for a live run."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible endpoint (openai provider)."),
    effort: str = typer.Option("medium", "--effort", help="Live model effort."),
    max_tasks: int = typer.Option(12, min=4),
    max_model_calls: int = typer.Option(20, min=0),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    """Resume the active paused or interrupted run."""

    root = path.resolve()
    if not _database(root).is_file():
        typer.echo("Not an autoresearch project.", err=True)
        raise typer.Exit(code=1)
    if provider not in PROVIDERS:
        typer.echo(f"--provider must be one of: {', '.join(PROVIDERS)}.", err=True)
        raise typer.Exit(code=2)
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        typer.echo("--effort must be low, medium, high, xhigh, or max.", err=True)
        raise typer.Exit(code=2)
    agent = FakeAgent() if fake else _live_agent(provider, model, effort, base_url, root)
    try:
        outcome = execute_project(
            root,
            agent,
            max_tasks=max_tasks,
            max_model_calls=max_model_calls,
            idempotency_key=idempotency_key,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"Resume failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Status: {outcome.value}")


@app.command("code")
def code(
    objective: str = typer.Argument(help="Implementation, debugging, build, or test assignment."),
    project: Path = typer.Option(Path.cwd(), "--project", help="Project whose files the coding worker may use."),
    input_paths: list[str] | None = typer.Option(None, "--input", help="Project-relative source path; repeat as needed."),
    job_id: str | None = typer.Option(None, "--job-id", help="Resume a previous coding job."),
    model: str | None = typer.Option(None, help="Codex coding model."),
    effort: str = typer.Option("high", help="Coding reasoning effort."),
    max_agents: int = typer.Option(3, min=0, max=8, help="Maximum coding delegates; 0 keeps one coding agent."),
    timeout: float = typer.Option(1800, min=1, help="Coding turn timeout in seconds."),
) -> None:
    """Invoke Codex only for a coding assignment; research stays with its own agents."""
    from ar.coding_agent import CodingAgent

    load_env(project.resolve() / ".env", Path.cwd() / ".env")
    worker = CodingAgent(project.resolve())
    try:
        job = worker.start(objective, input_paths=input_paths, job_id=job_id, model=model,
                           effort=effort, max_agents=max_agents, timeout=timeout)
        typer.echo(f"Coding job: {job['job_id']}")
        result = worker.wait(job["job_id"])
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(f"Coding job failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        worker.close()
    typer.echo(f"Status: {result['status']}")
    for artifact in result.get("result", {}).get("artifacts", []):
        typer.echo(f"Artifact: {artifact['path']} ({artifact['sha256']})")
    if result["status"] != "completed":
        if result.get("error"):
            typer.echo(result["error"], err=True)
        raise typer.Exit(code=1)


@app.command()
def studio(
    port: int = typer.Option(8765, help="Port on 127.0.0.1."),
    provider: str | None = typer.Option(None, help="openai or anthropic (default: whichever key is in .env)."),
    model: str | None = typer.Option(None, help="Model id for the configured research provider."),
    fake: bool = typer.Option(False, "--fake", help="Demo mode on the built-in papers, no API key."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab."),
) -> None:
    """Open the research chat with the existing research and literature agents."""
    from ar.studio.__main__ import main as studio_main

    args = ["--port", str(port)] + (["--provider", provider] if provider else []) + (["--model", model] if model else [])
    args += ["--fake"] if fake else []
    args += ["--no-browser"] if no_browser else []
    raise typer.Exit(studio_main(args))


if __name__ == "__main__":  # pragma: no cover
    app()

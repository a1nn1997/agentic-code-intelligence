"""agentctl command tree (Typer)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from acp import __version__
from acp.common.logging import configure_logging, get_logger
from acp.common.security import issue_api_key
from acp.config import get_settings
from acp.db import ApiKeysRepo, Database, UsersRepo, WorkspacesRepo, init_db
from acp.db.repositories import JournalRepo
from acp.index import IndexBuilder
from acp.retrieval import RetrievalServiceImpl
from acp.retrieval.interface import SnapshotRef, SpanRef
from acp.workspace import WorkspaceServiceImpl

app = typer.Typer(
    name="agentctl",
    help="Operator CLI for the Agentic Code-Intelligence Platform.",
    no_args_is_help=True,
    add_completion=False,
)
_log = get_logger(__name__)

# The demo user id is fixed so demos/eval are reproducible run-to-run.
DEMO_USER_ID = "user_demo"


@app.command()
def version() -> None:
    """Print the platform version."""
    typer.echo(__version__)


@app.command()
def config() -> None:
    """Print the resolved configuration (secrets redacted)."""
    s = get_settings()
    dump = json.loads(s.model_dump_json())
    dump["model_api_key"] = "<set>" if s.model_api_key.get_secret_value() else "<unset>"
    typer.echo(json.dumps(dump, indent=2, sort_keys=True))


@app.command()
def migrate() -> None:
    """Apply the SQLite schema (WAL mode). Idempotent."""
    s = get_settings()
    init_db(s.sqlite_path)
    typer.echo(f"migrated: {s.sqlite_path}")


@app.command()
def seed() -> None:
    """Create the demo user + a hashed API key. Prints the token ONCE.

    The raw token is never stored — only its prefix + sha256 hash land in the
    DB, which is exactly the property the Phase-0 oracle checks.
    """
    s = get_settings()
    init_db(s.sqlite_path)
    db = Database(s.sqlite_path)
    users, keys, workspaces = UsersRepo(db), ApiKeysRepo(db), WorkspacesRepo(db)

    if users.get(DEMO_USER_ID) is None:
        users.create(DEMO_USER_ID)
    issued = issue_api_key()
    keys.create(
        DEMO_USER_ID,
        issued.prefix,
        issued.key_hash,
        daily_token_budget=s.default_user_daily_token_budget,
    )
    # A placeholder workspace row so downstream demos have something to scope to.
    # (Real repo ingestion is Phase 1; this only exercises the DB layer.)
    if not workspaces.list(DEMO_USER_ID):
        workspaces.create(DEMO_USER_ID, source="seed://placeholder")
    db.close()

    typer.echo(f"seeded demo user: {DEMO_USER_ID}")
    typer.secho("API KEY (shown once, not stored raw):", fg=typer.colors.YELLOW)
    typer.secho(f"  {issued.token}", fg=typer.colors.GREEN, bold=True)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override gateway host"),
    port: int | None = typer.Option(None, help="Override gateway port"),
) -> None:
    """Run the gateway (uvicorn). This is what `make up` / compose invokes."""
    import uvicorn

    s = get_settings()
    configure_logging(s.log_level)
    uvicorn.run(
        "acp.gateway.app:app",
        host=host or s.gateway_host,
        port=port or s.gateway_port,
        log_config=None,  # keep our JSON logger; don't let uvicorn install its own
    )


@app.command()
def trace(task_id: str = typer.Argument(..., help="Task id to print the journal for")) -> None:
    """Print a task's journal — the per-run trace. (`make trace TASK=<id>`)"""
    s = get_settings()
    db = Database(s.sqlite_path)
    entries = JournalRepo(db).get_trace(task_id)
    db.close()
    if not entries:
        typer.echo(f"no journal entries for task {task_id}")
        raise typer.Exit(code=0)
    for e in entries:
        typer.echo(f"[{e.step_index:>3}] {e.kind:<9} {e.payload_json}")


# --- task: run the hand-written agent loop (Phase 4). Operator/internal; the
# --- consumer HTTP API + SSE is Phase 6. `task run` drives one task end to end
# --- against the stub model + real sandbox; `task resume` replays the journal. --
task_app = typer.Typer(help="Agent-loop task runs (operator/internal).", no_args_is_help=True)
app.add_typer(task_app, name="task")


def _orchestrator() -> Any:
    from acp.orchestrator import OrchestratorImpl
    from acp.sandbox_client import build_sandbox_client

    s = get_settings()
    init_db(s.sqlite_path)
    return OrchestratorImpl(
        Database(s.sqlite_path),
        s.workspace_root,
        build_sandbox_client(s),
        default_token_budget=s.default_task_token_budget,
        default_step_budget=s.default_task_step_budget,
        default_wall_clock_seconds=s.default_task_wall_clock_seconds,
    )


@task_app.command("run")
def task_run(
    instruction: str = typer.Argument(..., help="The task instruction (trusted channel)"),
    workspace: str = typer.Option(..., help="Workspace id to operate on"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
    max_tokens: int = typer.Option(0, help="Per-task token budget (0 = server default)"),
    max_steps: int = typer.Option(0, help="Per-task step budget (0 = server default)"),
) -> None:
    """Run the agent loop end to end and print the terminal state + metering."""
    from acp.orchestrator.interface import TaskRequest

    orch = _orchestrator()
    status = orch.submit(
        TaskRequest(
            user_id=user,
            workspace_id=workspace,
            instruction=instruction,
            max_tokens=max_tokens or None,
            max_steps=max_steps or None,
        )
    )
    typer.echo(json.dumps(json.loads(status.model_dump_json()), indent=2, sort_keys=True))
    raise typer.Exit(code=0 if status.state.value == "verified_success" else 1)


@task_app.command("resume")
def task_resume(
    task_id: str = typer.Argument(..., help="Task id to resume from its journal"),
) -> None:
    """Replay a crash-interrupted task's journal (effectively-once)."""
    status = _orchestrator().resume(task_id)
    typer.echo(json.dumps(json.loads(status.model_dump_json()), indent=2, sort_keys=True))


# --- workspace + index: operator-only inspection (Phase 1). NOT a consumer API;
# --- the model/consumer never reaches the index except via Phase-2 retrieval. --
workspace_app = typer.Typer(
    help="Workspace ingestion (operator/internal).", no_args_is_help=True
)
index_app = typer.Typer(
    help="Structural index inspection (operator/internal).", no_args_is_help=True
)
app.add_typer(workspace_app, name="workspace")
app.add_typer(index_app, name="index")


def _workspace_service() -> WorkspaceServiceImpl:
    s = get_settings()
    return WorkspaceServiceImpl(Database(s.sqlite_path), s.workspace_root)


@workspace_app.command("create")
def workspace_create(
    source: str = typer.Argument(..., help="Local dir or .zip/.tar.gz archive to ingest"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
) -> None:
    """Ingest a repo into a new user-scoped workspace and index it."""
    s = get_settings()
    init_db(s.sqlite_path)
    db = Database(s.sqlite_path)
    if UsersRepo(db).get(user) is None:  # ensure the FK target exists
        UsersRepo(db).create(user)
    svc = WorkspaceServiceImpl(db, s.workspace_root)
    ref = svc.create_workspace(user, source)
    idx = svc.build_index(user, ref.workspace_id)
    typer.echo(f"workspace: {ref.workspace_id}")
    typer.echo(f"head:      {ref.head_commit}")
    typer.echo(f"index:     {json.dumps(idx.stats(), sort_keys=True)}")


@workspace_app.command("list")
def workspace_list(user: str = typer.Option(DEMO_USER_ID, help="Owning user id")) -> None:
    """List a user's workspaces (only ever that user's)."""
    for w in _workspace_service().list_workspaces(user):
        typer.echo(f"{w.workspace_id}\t{w.head_commit}\t{w.source}")


@index_app.command("build")
def index_build(
    path: str | None = typer.Option(None, help="Ad-hoc: build index for a local dir"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id (workspace mode)"),
    workspace: str = typer.Option("", help="Workspace id (workspace mode)"),
) -> None:
    """Build the structural index and print stats + digest.

    ``--path`` builds an ad-hoc index for any directory (quick inspection);
    ``--workspace`` builds + persists the index for an ingested workspace.
    """
    if path is not None:
        idx = IndexBuilder().build(Path(path))
    elif workspace:
        idx = _workspace_service().build_index(user, workspace)
    else:
        typer.secho("provide --path DIR or --workspace ID", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(idx.stats(), indent=2, sort_keys=True))
    typer.echo(f"digest: {idx.digest()}")


@index_app.command("stats")
def index_stats(
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
    workspace: str = typer.Option(..., help="Workspace id"),
) -> None:
    """Load a workspace's persisted index and print its stats."""
    idx = _workspace_service().load_index(user, workspace)
    typer.echo(json.dumps(idx.stats(), indent=2, sort_keys=True))


@index_app.command("refs")
def index_refs(
    symbol: str = typer.Argument(..., help="Symbol name to resolve"),
    path: str | None = typer.Option(None, help="Ad-hoc: index a local dir first"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id (workspace mode)"),
    workspace: str = typer.Option("", help="Workspace id (workspace mode)"),
) -> None:
    """Resolve a symbol to its definition(s) and all call sites (both languages)."""
    if path is not None:
        idx = IndexBuilder().build(Path(path))
    elif workspace:
        idx = _workspace_service().load_index(user, workspace)
    else:
        typer.secho("provide --path DIR or --workspace ID", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    for d in idx.definitions(symbol):
        typer.echo(f"def  {d.language:<10} {d.file_path}:{d.start_line} ({d.kind})")
    for r in idx.references_of(symbol):
        typer.echo(f"ref  {r.language:<10} {r.file_path}:{r.line} ({r.ref_kind})")


# --- retrieval: budgeted, metered primitives for operator inspection (Phase 2).
# --- Internal/operator only — NOT a consumer HTTP API (that is Phase 6). Each
# --- command prints the token/byte cost so the metering is visible. ------------
retrieve_app = typer.Typer(
    help="Budgeted structural retrieval (operator/internal).", no_args_is_help=True
)
app.add_typer(retrieve_app, name="retrieve")


def _retrieval_service(user: str, workspace: str, budget: int) -> tuple[
    RetrievalServiceImpl, SnapshotRef
]:
    """Build a scope-bound RetrievalService + the workspace's current snapshot."""
    s = get_settings()
    db = Database(s.sqlite_path)
    ws = WorkspaceServiceImpl(db, s.workspace_root)
    ref = ws.get_workspace(user, workspace)  # ownership gate
    if ref.head_commit is None:
        typer.secho("workspace has no indexed snapshot; run workspace create", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    svc = RetrievalServiceImpl(
        db, s.workspace_root, user, scope=f"cli:{workspace}", budget_tokens=budget
    )
    return svc, SnapshotRef(workspace_id=workspace, commit=ref.head_commit)


@retrieve_app.command("search")
def retrieve_search(
    query: str = typer.Argument(..., help="Symbol-name query (substring, case-insensitive)"),
    workspace: str = typer.Option(..., help="Workspace id"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
    budget: int = typer.Option(1_000_000, help="Token budget ceiling for this session"),
) -> None:
    """search_symbols: find symbols by name across indexed languages."""
    svc, snap = _retrieval_service(user, workspace, budget)
    for sym in svc.search_symbols(snap, query):
        typer.echo(f"{sym.language:<10} {sym.kind:<9} {sym.file_path}:{sym.start_line} {sym.name}")
    typer.secho(f"[cost: {svc.spent_tokens()} tokens]", fg=typer.colors.CYAN)


@retrieve_app.command("refs")
def retrieve_refs(
    symbol: str = typer.Argument(..., help="Symbol name to resolve references for"),
    workspace: str = typer.Option(..., help="Workspace id"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
    budget: int = typer.Option(1_000_000, help="Token budget ceiling for this session"),
) -> None:
    """references: all call sites / uses of a symbol across files."""
    svc, snap = _retrieval_service(user, workspace, budget)
    d = svc.definition(snap, symbol)
    if d is not None:
        typer.echo(f"def  {d.language:<10} {d.file_path}:{d.start_line}-{d.end_line} ({d.kind})")
    for r in svc.references(snap, symbol):
        typer.echo(f"ref  {r.file_path}:{r.start_line}")
    typer.secho(f"[cost: {svc.spent_tokens()} tokens]", fg=typer.colors.CYAN)


@retrieve_app.command("read")
def retrieve_read(
    file: str = typer.Argument(..., help="Repo-relative file path"),
    workspace: str = typer.Option(..., help="Workspace id"),
    start: int = typer.Option(0, help="Start line (1-based); 0 = whole file"),
    end: int = typer.Option(0, help="End line (1-based, inclusive); 0 with start>0 = to EOF"),
    user: str = typer.Option(DEMO_USER_ID, help="Owning user id"),
    budget: int = typer.Option(1_000_000, help="Token budget ceiling for this session"),
) -> None:
    """read_span / read_file: read a span (cheap) or a whole file (costlier).

    Content is secret-redacted before display; the cost of the call is printed.
    """
    svc, snap = _retrieval_service(user, workspace, budget)
    if start > 0:
        res = svc.read_span(
            snap, SpanRef(file_path=file, start_line=start, end_line=end or 10**9)
        )
    else:
        res = svc.read_file(snap, file)
    typer.echo(res.content)
    typer.secho(
        f"[cost: {res.token_cost} tokens / {res.byte_count} bytes]", fg=typer.colors.CYAN
    )


# --- sandbox: real Docker-isolated verification (Phase 3). Operator/internal
# --- inspection only — NOT a consumer API. Shows the structured VerificationResult
# --- (the oracle) + resource usage for a patch against a repo snapshot. ---------
sandbox_app = typer.Typer(
    help="Sandboxed verification (operator/internal).", no_args_is_help=True
)
app.add_typer(sandbox_app, name="sandbox")

# Named model-free fixtures, so an operator can reproduce every oracle clause.
_FIXTURES = {
    "good": "good_patch",
    "bad": "bad_patch",
    "network": "network_patch",
    "infinite": "infinite_loop_patch",
    "oom": "oom_patch",
    "bounded-alloc": "bounded_alloc_patch",
    "unapplyable": "unapplyable_patch",
    "unbuildable": "unbuildable_patch",
    "empty": "empty_patch",
}


@sandbox_app.command("verify")
def sandbox_verify(
    source: str = typer.Option(..., help="Repo dir to snapshot + verify (e.g. ./sample_repo)"),
    patch: str = typer.Option(
        "empty",
        help=f"A fixture name ({', '.join(_FIXTURES)}) or a path to a patch-envelope JSON file",
    ),
) -> None:
    """Apply a patch to a snapshot of SOURCE and verify it IN THE SANDBOX.

    Prints the structured VerificationResult — the oracle downstream code trusts,
    never a model self-report — plus wall-clock resource usage.
    """
    import shutil
    import tempfile

    from acp.sandbox_client import build_sandbox_client, fixtures
    from acp.sandbox_client.interface import VerificationRequest

    if patch in _FIXTURES:
        patch_envelope = getattr(fixtures, _FIXTURES[patch])()
    else:
        p = Path(patch)
        if not p.is_file():
            typer.secho(
                f"patch must be a fixture name or a file path: {patch}", fg=typer.colors.RED
            )
            raise typer.Exit(code=2)
        patch_envelope = p.read_text(encoding="utf-8")

    src = Path(source)
    if not src.is_dir():
        typer.secho(f"source is not a directory: {source}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    client = build_sandbox_client()
    if not client.healthy():
        typer.secho(
            "sandbox not healthy: is Docker running and is the acp-sandbox image built? "
            "(run `make install` or `docker build -t acp-sandbox:latest sandbox`)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    with tempfile.TemporaryDirectory(prefix="acp-snap-") as tmp:
        snap = Path(tmp) / "repo"
        shutil.copytree(src, snap)
        req = VerificationRequest(
            task_id="cli", workspace_id="cli", base_commit="cli", patch=patch_envelope
        )
        result = client.verify_snapshot(req, snap)

    typer.echo(json.dumps(json.loads(result.model_dump_json()), indent=2, sort_keys=True))
    # Non-zero exit iff not verified, so `make`/scripts can gate on it.
    raise typer.Exit(code=0 if result.verified else 1)


# --- Demo / eval commands: stubbed until Phase 7, but present so the Makefile
# --- interface and command tree are complete now. -----------------------------
@app.command()
def eval(task: str = typer.Option("", "--task", help="Run a single eval task by id")) -> None:
    """Run the eval harness in stub mode (keyless).  Exit non-zero if any oracle fails."""
    from eval.runner import run_all

    task_ids = [task] if task else None
    code = run_all(task_ids)
    raise typer.Exit(code=code)


@app.command()
def redteam() -> None:
    """Run the injection + secret-exfil defense tasks only.  Exit non-zero if any fail."""
    from eval.runner import run_redteam

    code = run_redteam()
    raise typer.Exit(code=code)


@app.command("demo-happy")
def demo_happy() -> None:
    """Scripted happy-path walkthrough: index sample_repo → run task → verified_success."""
    from eval.demo import run_demo_happy

    run_demo_happy()


@app.command("demo-resume")
def demo_resume() -> None:
    """Walkthrough: start a task, kill it mid-run, resume → verified_success, no double-charge."""
    from eval.demo import run_demo_resume

    run_demo_resume()


@app.command("demo-budget")
def demo_budget() -> None:
    """Walkthrough: budget-constrained task → clean stop at budget_exhausted + partial report."""
    from eval.demo import run_demo_budget

    run_demo_budget()


def main() -> None:
    """Console-script entrypoint (``agentctl``)."""
    app()


if __name__ == "__main__":
    main()

"""agentctl: version, migrate, and seed. Seed is the operator path that must
store keys hashed-only and reveal the token exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp.cli.main import DEMO_USER_ID, app
from acp.config import get_settings
from acp.db.connection import Database

pytestmark = pytest.mark.unit

runner = CliRunner()

# The committed sample repo lives at the project root.
SAMPLE_REPO_SRC = Path(__file__).resolve().parents[2] / "sample_repo"


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the CLI's settings at a temp DB via env, and reset the cache."""
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("ACP_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ACP_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ACP_MODEL_BACKEND", "stub")
    # Force keyless regardless of any local .env: real environment variables take
    # precedence over the .env file in pydantic-settings, so this pins the state.
    monkeypatch.setenv("ACP_MODEL_API_KEY", "")
    get_settings.cache_clear()
    yield str(db_path)
    get_settings.cache_clear()


def test_version(cli_env: str) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_seed_creates_user_and_hashed_key(cli_env: str) -> None:
    result = runner.invoke(app, ["seed"])
    assert result.exit_code == 0, result.stdout
    assert DEMO_USER_ID in result.stdout
    # The full token is shown once in the output...
    assert "." in result.stdout  # token is "<prefix>.<secret>"

    # ...but the DB stores only prefix + hash, never the raw secret.
    db = Database(cli_env)
    row = db.conn.execute("SELECT key_prefix, key_hash FROM api_keys;").fetchone()
    db.close()
    assert row is not None
    assert len(row["key_hash"]) == 64  # sha256 hex
    assert row["key_prefix"] not in row["key_hash"]


def test_config_redacts_key(cli_env: str) -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert '"model_api_key": "<unset>"' in result.stdout


def test_index_build_ad_hoc_on_sample_repo(cli_env: str) -> None:
    result = runner.invoke(app, ["index", "build", "--path", str(SAMPLE_REPO_SRC)])
    assert result.exit_code == 0, result.stdout
    assert '"files_python"' in result.stdout
    assert '"files_typescript"' in result.stdout
    assert "digest:" in result.stdout


def test_index_refs_resolves_cross_file_symbol(cli_env: str) -> None:
    result = runner.invoke(
        app, ["index", "refs", "serialize_user", "--path", str(SAMPLE_REPO_SRC)]
    )
    assert result.exit_code == 0, result.stdout
    assert "def  python" in result.stdout
    # references surface in api.py, export.py and the tests
    assert "backend/app/users/api.py" in result.stdout
    assert "backend/app/reports/export.py" in result.stdout


def test_workspace_create_and_index_stats(cli_env: str) -> None:
    created = runner.invoke(
        app, ["workspace", "create", str(SAMPLE_REPO_SRC), "--user", "user_cli"]
    )
    assert created.exit_code == 0, created.stdout
    ws_line = next(line for line in created.stdout.splitlines() if line.startswith("workspace:"))
    ws_id = ws_line.split(":", 1)[1].strip()

    stats = runner.invoke(
        app, ["index", "stats", "--user", "user_cli", "--workspace", ws_id]
    )
    assert stats.exit_code == 0, stats.stdout
    assert '"resolved_import_edges"' in stats.stdout

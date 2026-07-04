"""Shared fixtures: an isolated temp-DB ``Settings`` and an initialized DB.

Every test gets its own SQLite file under a tmp dir, so tests never share state
and can run in any order (and in parallel later).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from acp.config.settings import Settings
from acp.db.connection import Database, init_db

# The synthetic polyglot fixture repo, at the project root.
SAMPLE_REPO_SRC = Path(__file__).resolve().parents[1] / "sample_repo"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Keyless stub-mode settings pointed at a throwaway SQLite file.

    ``_env_file=None`` makes the fixture hermetic: tests must never pick up a
    developer's local ``.env`` (which may set a real key or host) — that would
    make results depend on machine state.
    """
    db_path = tmp_path / "acp_test.db"
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite:///{db_path}",
        model_backend="stub",
        env="test",
    )


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    """A migrated database on the temp settings."""
    init_db(settings.sqlite_path)
    database = Database(settings.sqlite_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A per-test *mutable copy* of the sample repo, so incremental/edit tests
    never touch the committed fixture."""
    dest = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO_SRC, dest)
    return dest


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A throwaway per-user workspace root for WorkspaceService tests."""
    return tmp_path / "workspaces"

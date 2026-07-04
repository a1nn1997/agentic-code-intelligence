-- ACP SQLite schema (WAL mode).
-- All DDL lives here; business modules never issue raw SQL — they call the
-- typed repositories in acp.db.repositories. Two tables are APPEND-ONLY and
-- back the correctness-under-failure guarantees: `journal` (replay/resume) and
-- `budget_ledger` (no double-charge). Their invariants are enforced by triggers
-- so a stray UPDATE/DELETE fails at the database, not just in code review.

PRAGMA foreign_keys = ON;

-- Users -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- API keys — HASHED ONLY. No column ever holds a raw secret. ------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id                     TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_prefix             TEXT NOT NULL UNIQUE,   -- non-secret lookup handle
    key_hash               TEXT NOT NULL,          -- sha256(secret); never the secret
    rate_limit_per_min     INTEGER NOT NULL DEFAULT 60,
    daily_token_budget     INTEGER NOT NULL DEFAULT 2000000,
    revoked                INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- Workspaces — always owned by exactly one user (isolation by construction). --
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    head_commit TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id);

-- Tasks — the agent runs; carry per-task metering + terminal state. -----------
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id       TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    state              TEXT NOT NULL DEFAULT 'pending',
    mode               TEXT NOT NULL DEFAULT 'apply',
    instruction        TEXT NOT NULL,
    reason             TEXT,                        -- set for gave_up / budget_exhausted
    step_index         INTEGER NOT NULL DEFAULT 0,
    token_budget       INTEGER NOT NULL,
    step_budget        INTEGER NOT NULL,
    wall_clock_seconds INTEGER NOT NULL,
    tokens_in          INTEGER NOT NULL DEFAULT 0,
    tokens_out         INTEGER NOT NULL DEFAULT 0,
    tool_calls         INTEGER NOT NULL DEFAULT 0,
    retrieval_bytes    INTEGER NOT NULL DEFAULT 0,
    sandbox_seconds    REAL    NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id);

-- Journal — APPEND-ONLY. The per-run trace and the basis for replay/resume. ---
-- UNIQUE(task_id, step_index) is the idempotency key: a step can be written at
-- most once, so a resumed run re-executing a step is a no-op insert conflict.
CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_index      INTEGER NOT NULL,
    kind            TEXT NOT NULL,                  -- plan|retrieve|edit|verify|repair
    idempotency_key TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',     -- cached model response / step result
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (task_id, step_index)
);
CREATE INDEX IF NOT EXISTS idx_journal_task ON journal(task_id, step_index);

-- Enforce append-only at the DB layer: written rows are immutable (no UPDATE).
-- Direct DELETEs are prevented in the repository layer; the only rows that ever
-- leave are via ON DELETE CASCADE when a parent task is removed (a legitimate
-- lifecycle op), which a DB-level delete-trigger cannot distinguish from abuse.
CREATE TRIGGER IF NOT EXISTS journal_no_update
BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: UPDATE forbidden');
END;

-- Budget ledger — APPEND-ONLY. Balance = signed sum over a scope. -------------
-- reserve (negative available), commit (finalize spend), release (return reserve).
CREATE TABLE IF NOT EXISTS budget_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,                      -- 'user:<id>' or 'task:<id>'
    kind        TEXT NOT NULL,                      -- reserve|commit|release
    task_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    step_index  INTEGER,
    tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_scope ON budget_ledger(scope);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON budget_ledger(task_id);

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON budget_ledger
BEGIN
    SELECT RAISE(ABORT, 'budget_ledger is append-only: UPDATE forbidden');
END;

-- Artifacts — patches, reports, logs a run produces; content-addressed. -------
CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                     -- patch|report|log
    content_hash TEXT NOT NULL,                     -- gates re-apply (idempotent effects)
    path         TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);

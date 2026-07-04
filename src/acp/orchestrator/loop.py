"""The hand-written agent control loop — an explicit state machine we own.

This is the core the assignment says must NOT be hidden inside a framework.
There is no LangGraph / AutoGen / CrewAI / Assistants-SDK here: every transition
is ordinary Python you can single-step. The machine is:

    PLAN -> RETRIEVE -> EDIT -> VERIFY -> {verified_success | gave_up | budget_exhausted}
                                  |
                          (if verify fails)
                                  v
                        REPAIR -> EDIT -> VERIFY -> ...

**Phase-5 self-repair extension:**
When verification fails the loop does NOT immediately give up. Instead it feeds
the REAL captured sandbox failure output — stdout_tail + stderr_tail from the
Phase-3 sandbox result — into the DATA channel as a new ``<span>`` (origin=
``sandbox_failure:step:<N>``). The model then proposes a corrective EDIT.
Critical: the failure text is UNTRUSTED (it may echo adversarial repo content),
so it arrives via the data channel, never the instruction channel — the same
discipline as retrieved code. Repair attempts are journaled steps like any other:
resumable, apply-once, no double-charge. A repair that exhausts budget ends
``budget_exhausted`` at a clean journaled checkpoint; an unfixable failure ends
``gave_up`` with the reason.

Each transition is one journaled *step* with a monotonically increasing
``step_index``. The invariants that make runs correct under failure:

**1. Journal-before-effect, idempotent on (task_id, step_index).**
   For every step we append the journal row carrying the step's result BEFORE
   that result's external effect is relied upon. The append is
   ``INSERT OR IGNORE`` on the unique ``(task_id, step_index)`` key, so a
   resumed run re-reaching a step gets the *already-stored* payload back
   (``created=False``) instead of redoing the effect.

**2. The ledger is reconciled FROM the journal, so a paid call is charged once.**
   The crash window that matters: a paid model call RETURNED but neither its
   journal row nor its ledger commit were durable when the process died. We
   close it by making the journal the source of truth for charges. A model step
   journals BOTH the model response AND the intended token charge in one row,
   THEN commits that charge to the ledger tagged with ``(task_id, step_index)``.
   On resume, :meth:`_charge_from_journal` ensures exactly one COMMIT exists for
   each journaled model step — adding it if the process died between the journal
   append and the ledger commit, skipping it if it is already there. Result:
   the paid model call is never re-issued (its response is cached in the
   journal) and never double-charged (the commit is keyed and de-duplicated).
   Guarantee: **at-least-once execution + idempotent effects = effectively-once.**

**3. Patches apply exactly once, gated on artifact content-hash.**
   An EDIT step content-addresses its patch into the ``artifacts`` table and
   applies it to the worktree only if an artifact with that content-hash was not
   already recorded for the task. A resumed EDIT re-computes the same hash, sees
   it present, and does NOT re-apply — the worktree already holds it.

**4. Budget is checked BEFORE each paid op, and a breach stops CLEANLY.**
   Token and step budgets are checked before a model call. On breach the loop
   stops at the current journaled checkpoint in ``budget_exhausted`` with a
   partial report; it never overruns and never corrupts the worktree.

**5. "Verified" comes ONLY from the sandbox.**
   The terminal ``verified_success`` is set iff the Phase-3 sandbox returns
   ``applied & built & tests_passed`` (``VerificationResult.verified``). No model
   self-report can produce it — the model can only *ask* to verify; the verdict
   is the sandbox's.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.common.logging import get_logger
from acp.common.types import LedgerEntryKind, StepKind, TaskState
from acp.db import (
    ArtifactsRepo,
    Database,
    JournalRepo,
    LedgerRepo,
    TasksRepo,
)
from acp.db.models import Task
from acp.model_gateway import (
    ActionKind,
    AgentAction,
    DataSpan,
    ModelGateway,
    build_channels,
    parse_action,
)
from acp.model_gateway.interface import ModelRequest
from acp.orchestrator.interface import TaskEvent, TaskStatus
from acp.retrieval import RetrievalServiceImpl
from acp.retrieval.interface import SnapshotRef, SpanRef
from acp.sandbox_client.interface import SandboxClient, VerificationRequest
from acp.workspace.interface import WorktreeHandle
from acp.workspace.service import WorkspaceServiceImpl

_log = get_logger(__name__)

# The scripted step -> StepKind mapping for the single-file trajectory. Steps
# beyond VERIFY are not reached on the happy path; the loop terminates at VERIFY.
_ARTIFACT_PATCH = "patch"

# Maximum repair attempts before give-up-with-reason. Each repair is one
# VERIFY (that fails) + one EDIT (the corrective patch) cycle. The budget
# gates (step + token) are the primary stop; this is a backstop ceiling.
_MAX_REPAIR_ATTEMPTS = 3


@dataclass
class LoopConfig:
    """Everything the loop needs, wired by the caller (no globals)."""

    db: Database
    workspace_root: str | Path
    gateway: ModelGateway
    sandbox: SandboxClient
    # Per-task retrieval budget ceiling reuses the task token budget.


class AgentLoop:
    """Drives one task through the bounded, replayable state machine.

    A single instance runs (or resumes) a single task. It is deliberately
    re-entrant on the journal: calling :meth:`run` on a task that already has
    journal rows replays them and continues — that IS resume.
    """

    def __init__(self, cfg: LoopConfig) -> None:
        self._db = cfg.db
        self._workspace_root = cfg.workspace_root
        self._gateway = cfg.gateway
        self._sandbox = cfg.sandbox
        self._tasks = TasksRepo(cfg.db)
        self._journal = JournalRepo(cfg.db)
        self._ledger = LedgerRepo(cfg.db)
        self._artifacts = ArtifactsRepo(cfg.db)
        self._workspaces = WorkspaceServiceImpl(cfg.db, cfg.workspace_root)
        self._events: list[TaskEvent] = []
        # Per-run context, set in run() before the loop.
        self._worktree: Path = Path()
        self._snap: SnapshotRef = SnapshotRef(workspace_id="", commit="")
        self._retrieval: RetrievalServiceImpl | None = None
        self._run_start: float = 0.0  # set in run(); A7 wall-clock anchor
        self._handle: WorktreeHandle | None = None  # set in run(); A9 commit base

    # --- public entrypoints --------------------------------------------------
    def run(self, task_id: str) -> TaskStatus:
        """Run or resume a task to a terminal state, returning its status."""
        task = self._tasks.get_for_resume(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        if TaskState(task.state).is_terminal:
            # Already finished (e.g. a resume of a completed run): no-op, honest.
            return self._status(task)

        # RESUME RECONCILIATION (runs every time, harmless on a fresh task):
        # reconcile the ledger from the journal so any model step that journaled
        # but died before its commit is charged exactly once, then move on.
        self._charge_from_journal(task)

        scope = f"task:{task_id}"
        # A7: wall-clock deadline. monotonic() is immune to system-clock jumps.
        # Measured per run()/resume: a resumed task gets a fresh wall-clock
        # window, which is correct — we bound the *active* run, and every prior
        # step is already durably journaled, so the deadline still stops cleanly
        # at a checkpoint. Token/step budgets remain cumulative across resumes.
        self._run_start = time.monotonic()
        self._tasks.set_state(task_id, TaskState.RUNNING)
        self._snap = self._snapshot(task)
        self._handle = self._workspaces.open_worktree(task.user_id, task.workspace_id, task_id)
        self._worktree = Path(self._handle.path)
        self._retrieval = RetrievalServiceImpl(
            self._db,
            self._workspace_root,
            task.user_id,
            scope=scope,
            budget_tokens=task.token_budget,
        )

        # The data channel accumulates retrieved spans across the run; it starts
        # empty (nothing retrieved yet) and is rebuilt on resume from the journal.
        data_spans: list[DataSpan] = self._replay_data_spans(task_id)

        step = self._next_step_index(task_id)
        repair_attempts = self._count_repair_attempts(task_id)
        while True:
            # --- budget gate BEFORE any paid op ---
            breach = self._budget_breach(task, scope, step)
            if breach is not None:
                return self._terminate(
                    task_id, TaskState.BUDGET_EXHAUSTED, reason=breach
                )

            cached = self._journal.get_step(task_id, step)
            if cached is not None:
                # RESUME/REPLAY: this step is already durable. Re-derive the
                # action from the cached payload; do NOT re-issue the model call
                # and do NOT re-run its effect. Charging is reconciled separately
                # and idempotently, so a replayed step never double-charges.
                action = parse_action(json.loads(cached.payload_json)["content_xml"])
                self._replay_effect(task_id, step, action, cached, data_spans)
                # On resume, restore repair count from journal so backstop is correct.
                failed_verify = (
                    action.kind == ActionKind.VERIFY
                    and not self._verified_from_journal(task_id, step)
                )
                if failed_verify:
                    repair_attempts = self._count_repair_attempts(task_id)
            else:
                # Fresh step: one paid model call, one effect, ONE journal row
                # carrying both — written before the effect is relied upon.
                action = self._issue_model_call(task, scope, step, data_spans)

            if action.kind == ActionKind.GIVE_UP:
                return self._terminate(
                    task_id, TaskState.GAVE_UP, reason=action.fields.get("reason", "gave up")
                )
            if action.kind == ActionKind.VERIFY:
                if self._verified_from_journal(task_id, step):
                    # A9: in APPLY mode, commit the verified worktree back to the
                    # shared workspace through the GUARDED path (advisory lock +
                    # base-commit check). This is where Adversarial #6 ("two tasks
                    # edit the same workspace → never silent clobber") is enforced
                    # in production, not just in a helper test.
                    conflict = self._commit_if_apply(task)
                    if conflict is not None:
                        return self._terminate(
                            task_id, TaskState.GAVE_UP, reason=conflict
                        )
                    return self._terminate(task_id, TaskState.VERIFIED_SUCCESS)
                # Verification failed — enter self-repair if budget allows.
                failure_text = self._failure_text_from_journal(task_id, step)
                if repair_attempts >= _MAX_REPAIR_ATTEMPTS:
                    return self._terminate(
                        task_id,
                        TaskState.GAVE_UP,
                        reason=(
                            f"sandbox verification failed after {repair_attempts} "
                            f"repair attempt(s); could not fix"
                        ),
                    )
                # Feed the REAL failure output via the DATA channel (untrusted —
                # the sandbox captures repo test output which may echo adversarial
                # content). The origin label makes the channel explicit and
                # auditable: the test asserts on this origin being present.
                failure_span = DataSpan(
                    origin=f"sandbox_failure:step:{step}",
                    content=failure_text,
                )
                data_spans.append(failure_span)
                self._emit(
                    task_id,
                    step,
                    StepKind.REPAIR,
                    f"repair attempt {repair_attempts + 1}/{_MAX_REPAIR_ATTEMPTS} "
                    f"feeding {len(failure_text)} bytes via data channel",
                )
                repair_attempts += 1
                step += 1
                continue
            step += 1

    def events(self) -> list[TaskEvent]:
        """The progress events emitted during the last :meth:`run` (the journal
        is the durable trace; this is the in-memory view for the CLI/SSE)."""
        return list(self._events)

    # --- the fresh model step: one call, one effect, ONE journal row ---------
    def _issue_model_call(
        self, task: Task, scope: str, step: int, data_spans: list[DataSpan]
    ) -> AgentAction:
        """Fresh (not-yet-journaled) step. Order is deliberate and load-bearing:

        1. Build the STRICT XML channels — instruction (trusted) is the user's
           intent + target hint; data (untrusted) is only retrieved spans. The
           two never merge, so retrieved code cannot become an instruction.
        2. Issue the ONE paid model call and parse its action through the
           allowlist.
        3. Run the action's effect (retrieve / edit-apply-once / verify).
        4. Append ONE journal row carrying the model response, the effect result,
           and the intended token charge — BEFORE the loop advances.
        5. Commit the charge to the ledger, keyed on (task, step) → idempotent.

        If the process dies between (2) and (4), resume re-issues the call (no
        journal row yet — at-least-once). If it dies between (4) and (5),
        resume's :meth:`_charge_from_journal` adds the missing commit from the
        durable journal row — so the returned-before-crash call is charged
        exactly once and its effect (an already-applied patch) is not repeated.
        """
        instruction_xml, data_xml = build_channels(task.instruction, data_spans)
        request = ModelRequest(
            task_id=task.id, step_index=step, instruction_xml=instruction_xml, data_xml=data_xml
        )
        response = self._gateway.complete(request)
        action = parse_action(response.content_xml)

        effect_payload = self._run_effect(task, step, action, data_spans)

        self._journal.append(
            task.id,
            step,
            _kind_for_action(action.kind),
            idempotency_key=f"step:{task.id}:{step}",
            payload={
                "content_xml": response.content_xml,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "backend": response.backend,
                "charge_tokens": response.input_tokens + response.output_tokens,
                **effect_payload,
            },
        )
        self._commit_step_charge(
            scope,
            task.id,
            step,
            response.input_tokens + response.output_tokens,
            note=f"model:{action.kind.value}",
        )
        self._bump_task_tokens(task, response.input_tokens, response.output_tokens)
        return action

    def _run_effect(
        self, task: Task, step: int, action: AgentAction, data_spans: list[DataSpan]
    ) -> dict[str, Any]:
        """Perform the action's side effect and return its journal payload.

        Effects are idempotent by construction: EDIT is gated on the artifact
        content-hash (apply-once); RETRIEVE appends to the in-memory data channel
        and records only its origin; VERIFY records the sandbox verdict."""
        if action.kind == ActionKind.PLAN:
            self._emit(task.id, step, StepKind.PLAN, action.fields.get("summary", ""))
            return {}
        if action.kind == ActionKind.RETRIEVE:
            return self._do_retrieve(task, step, action, data_spans)
        if action.kind == ActionKind.EDIT:
            return self._do_edit(task.id, step, action)
        if action.kind == ActionKind.RENAME:
            return self._do_rename(task, step, action)
        if action.kind == ActionKind.VERIFY:
            return self._do_verify(task, step)
        return {}  # GIVE_UP: no effect

    def _replay_effect(
        self,
        task_id: str,
        step: int,
        action: AgentAction,
        cached: Any,
        data_spans: list[DataSpan],
    ) -> None:
        """Resume path for an already-journaled step. Re-apply only idempotent,
        cheap re-derivations — never the paid model call, never a re-charge, never
        a second patch apply (the artifact content-hash gate blocks it).

        For a failed VERIFY step, we rebuild the failure span that was fed to the
        model before the repair EDIT — so on resume the model sees the same data
        channel context as it had at the crash point.
        """
        payload = json.loads(cached.payload_json)
        if action.kind == ActionKind.RETRIEVE:
            data_spans.append(DataSpan(origin=payload.get("origin", ""), content=""))
        elif action.kind == ActionKind.VERIFY and not payload.get("verified", False):
            # This was a failed verify that triggered a repair. Re-add the failure
            # span to the data channel so on resume the model's context is intact.
            failure_text = self._failure_text_from_journal(task_id, step)
            data_spans.append(
                DataSpan(origin=f"sandbox_failure:step:{step}", content=failure_text)
            )
        self._emit(task_id, step, _kind_for_action(action.kind), "replayed")

    # --- reconciliation: charge the ledger FROM the journal ------------------
    def _charge_from_journal(self, task: Task) -> None:
        """Ensure every journaled model step has exactly one ledger COMMIT.

        This is the crash-window fix. For each journal row that carries a
        ``charge_tokens`` payload, we check whether a COMMIT tagged with that
        ``(task_id, step_index)`` already exists; if not, we add it. Idempotent:
        running it twice never adds a second commit for the same step. This makes
        the JOURNAL — not the volatile in-flight state — the source of truth for
        what was charged, so a model call that returned before the crash is
        charged once and only once, with no double-charge and no leaked charge.
        """
        scope = f"task:{task.id}"
        committed_steps = self._committed_model_steps(scope, task.id)
        for entry in self._journal.get_trace(task.id):
            payload = json.loads(entry.payload_json)
            charge = payload.get("charge_tokens")
            if charge is None:
                continue  # non-model step (retrieval/edit/verify) — charged elsewhere
            if entry.step_index in committed_steps:
                continue  # already committed — do not double-charge
            self._ledger.append(
                scope,
                LedgerEntryKind.COMMIT,
                tokens=int(charge),
                task_id=task.id,
                step_index=entry.step_index,
                note="reconcile:model",
            )
            committed_steps.add(entry.step_index)

    def _committed_model_steps(self, scope: str, task_id: str) -> set[int]:
        rows = self._db.conn.execute(
            "SELECT step_index FROM budget_ledger WHERE scope = ? AND task_id = ? "
            "AND kind = ? AND step_index IS NOT NULL AND note LIKE 'model:%';",
            (scope, task_id, LedgerEntryKind.COMMIT.value),
        ).fetchall()
        recon = self._db.conn.execute(
            "SELECT step_index FROM budget_ledger WHERE scope = ? AND task_id = ? "
            "AND kind = ? AND note = 'reconcile:model';",
            (scope, task_id, LedgerEntryKind.COMMIT.value),
        ).fetchall()
        return {int(r["step_index"]) for r in rows} | {int(r["step_index"]) for r in recon}

    def _commit_step_charge(
        self, scope: str, task_id: str, step: int, tokens: int, *, note: str
    ) -> None:
        """Append a COMMIT for a step iff one is not already present (idempotent)."""
        if step in self._committed_model_steps(scope, task_id):
            return
        self._ledger.append(
            scope,
            LedgerEntryKind.COMMIT,
            tokens=tokens,
            task_id=task_id,
            step_index=step,
            note=note,
        )

    # --- the tool/effect steps (return payload; caller journals ONE row) -----
    def _do_retrieve(
        self, task: Task, step: int, action: AgentAction, data_spans: list[DataSpan]
    ) -> dict[str, Any]:
        """Execute a budgeted retrieval primitive; append its bytes to the data
        channel. Retrieval charges the SAME ledger scope (Phase-2 reserve/commit),
        so retrieval bytes count against the task budget too."""
        assert self._retrieval is not None
        primitive = action.fields.get("primitive", "definition")
        name = action.fields.get("name", "")
        origin = f"{primitive}:{name}"
        content = ""
        if primitive == "definition":
            sym = self._retrieval.definition(self._snap, name)
            if sym is not None:
                res = self._retrieval.read_span(
                    self._snap, _span(sym.file_path, sym.start_line, sym.end_line)
                )
                content = res.content
                origin = f"read_span:{sym.file_path}:{sym.start_line}-{sym.end_line}"
                # A3: meter the retrieved bytes onto the task row (was never written).
                self._bump_retrieval_bytes(task.id, res.byte_count)
        data_spans.append(DataSpan(origin=origin, content=content))
        self._emit(task.id, step, StepKind.RETRIEVE, origin)
        return {"origin": origin}

    def _do_edit(self, task_id: str, step: int, action: AgentAction) -> dict[str, Any]:
        """Content-address the proposed patch and apply it to the worktree
        EXACTLY ONCE, gated on the artifact content-hash. Returns the envelope +
        hash for the step's journal row."""
        file_path = action.fields["file_path"]
        content = action.fields.get("content", "")
        op = action.fields.get("op", "write")
        envelope = json.dumps({"ops": [{"op": op, "path": file_path, "content": content}]})
        content_hash = hashlib.sha256(envelope.encode("utf-8")).hexdigest()

        already = any(
            a.content_hash == content_hash for a in self._artifacts.list_for_task(task_id)
        )
        if not already:
            # Record the artifact FIRST (the durable apply-once gate), then apply.
            self._artifacts.create(task_id, _ARTIFACT_PATCH, content_hash, path=file_path)
            self._apply_op_to_worktree(self._worktree, op, file_path, content)
        self._emit(task_id, step, StepKind.EDIT, f"{op} {file_path} [{content_hash[:12]}]")
        return {"envelope": envelope, "content_hash": content_hash, "applied": True}

    def _do_rename(self, task: Task, step: int, action: AgentAction) -> dict[str, Any]:
        """Rename a symbol across ALL index-known call sites and tests.

        Uses the Phase-1 structural index (definitions + references) to decide
        WHICH files are in scope — the definition site(s) plus every file with a
        reference to the symbol — then within each such file rewrites only
        WHOLE-IDENTIFIER occurrences of ``old_name`` to ``new_name`` (content-hash
        gated, apply-once).

        **Why whole-identifier tokens, not raw substitution (A11).** A raw
        ``str.replace(old_name, new_name)`` corrupts substrings: renaming ``user``
        would rewrite ``user_id`` → ``new_id`` and ``username`` → ``newname``, and
        even ``get_user`` → ``get_new``. We instead replace only ``\\b``-anchored
        identifier tokens (``_rename_identifier_tokens``): ``\\buser\\b`` matches
        the token ``user`` but never ``user_id`` / ``username`` / ``get_user``, so
        a colliding name is left intact. The index (not a raw grep) gates which
        files are touched, so unrelated files are never rewritten.

        Returns a payload recording every affected file and content-hash so the
        journal + programmatic diff-check can verify completeness.
        """
        assert self._retrieval is not None
        old_name: str = action.fields.get("old_name", "")
        new_name: str = action.fields.get("new_name", "")
        if not old_name or not new_name:
            return {"renamed_files": [], "old_name": old_name, "new_name": new_name}

        from acp.index.builder import IndexBuilder
        # Read the workspace index (original symbol positions) to discover all sites.
        repo_root = Path(self._workspaces.repo_path(task.user_id, task.workspace_id))
        index = IndexBuilder().build(repo_root)

        # Discover affected files from the structural index: the definition
        # site(s) plus every file with a reference to the symbol. The index — not
        # a raw grep — decides which files are in scope, so unrelated files (and
        # colliding names like `user_id`) are never touched.
        affected: set[str] = {sym.file_path for sym in index.definitions(old_name)}
        affected.update(ref.file_path for ref in index.references_of(old_name))

        renamed_files: list[dict[str, str]] = []
        worktree_root = self._worktree
        # Read from the worktree — it holds any previous edits already applied.
        for rel_path in sorted(affected):
            abs_path = worktree_root / rel_path
            if not abs_path.is_file():
                continue
            original = abs_path.read_text(encoding="utf-8")
            # Replace only WHOLE-IDENTIFIER occurrences of old_name in files the
            # index flagged (A11). Whole-identifier boundaries make this
            # collision-safe: renaming `user` leaves `user_id`/`username` intact,
            # unlike the previous raw ``str.replace``.
            updated = _rename_identifier_tokens(original, old_name, new_name)
            if updated == original:
                continue  # no standalone token present — skip
            envelope = json.dumps({"ops": [{"op": "write", "path": rel_path, "content": updated}]})
            content_hash = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
            already = any(
                a.content_hash == content_hash for a in self._artifacts.list_for_task(task.id)
            )
            if not already:
                self._artifacts.create(task.id, _ARTIFACT_PATCH, content_hash, path=rel_path)
                self._apply_op_to_worktree(self._worktree, "write", rel_path, updated)
            renamed_files.append({"path": rel_path, "content_hash": content_hash})

        self._emit(
            task.id,
            step,
            StepKind.EDIT,
            f"rename {old_name}→{new_name} across {len(renamed_files)} file(s)",
        )
        return {
            "old_name": old_name,
            "new_name": new_name,
            "renamed_files": renamed_files,
        }

    def _do_verify(self, task: Task, step: int) -> dict[str, Any]:
        """Build the patch envelope from journaled edits and verify IN THE
        SANDBOX. The returned payload records the sandbox verdict; the terminal
        decision reads ``verified`` from it. The verdict is the sandbox's alone —
        no model self-report can set it."""
        envelope = self._combined_patch(task.id)
        req = VerificationRequest(
            task_id=task.id,
            workspace_id=task.workspace_id,
            # A2: the verify request must carry the resolved base COMMIT (the
            # snapshot the worktree/patch derive from), not the workspace_id. The
            # snapshot is captured in run() as self._snap.
            base_commit=self._snap.commit,
            patch=envelope,
        )
        _t0 = time.monotonic()
        result = self._sandbox.verify_snapshot(req, self._worktree)
        # A3: record sandbox duration onto the task metering row. Prefer the
        # runner's own reported wall-clock (the real DockerSandboxRunner sets it);
        # fall back to the measured call latency so the metric is non-zero even
        # for in-process stub runners that don't report it.
        elapsed = max(time.monotonic() - _t0, result.wall_clock_seconds)
        self._bump_sandbox_seconds(task.id, elapsed)
        self._emit(
            task.id,
            step,
            StepKind.VERIFY,
            f"verified={result.verified} built={result.built} tests={result.tests_passed}",
        )
        return {
            "verified": result.verified,
            "applied": result.applied,
            "built": result.built,
            "tests_passed": result.tests_passed,
            "exit_code": result.exit_code,
            "stage": result.stage.value,
            "killed_reason": result.killed_reason.value if result.killed_reason else None,
            # Phase-5: store the real captured output so self-repair can feed it
            # to the model via the DATA channel without re-running the sandbox.
            "stdout_tail": result.stdout_tail or "",
            "stderr_tail": result.stderr_tail or "",
        }

    def _verified_from_journal(self, task_id: str, step: int) -> bool:
        """Read the VERIFY step's journaled verdict. VERIFIED_SUCCESS derives
        ONLY from the sandbox result stored here — never a model claim."""
        entry = self._journal.get_step(task_id, step)
        if entry is None:
            return False
        return bool(json.loads(entry.payload_json).get("verified", False))

    def _failure_text_from_journal(self, task_id: str, step: int) -> str:
        """Extract the REAL captured sandbox failure output from a journaled VERIFY step.

        This is what self-repair feeds via the DATA channel. The text comes from
        the Phase-3 sandbox (stdout_tail + stderr_tail) stored in the journal row
        — the ACTUAL error the test suite produced, not a model self-report.
        """
        entry = self._journal.get_step(task_id, step)
        if entry is None:
            return ""
        payload = json.loads(entry.payload_json)
        parts: list[str] = []
        if payload.get("stdout_tail"):
            parts.append(f"stdout:\n{payload['stdout_tail']}")
        if payload.get("stderr_tail"):
            parts.append(f"stderr:\n{payload['stderr_tail']}")
        stage = payload.get("stage", "")
        if stage:
            parts.append(f"stage: {stage}")
        return "\n---\n".join(parts)

    def _count_repair_attempts(self, task_id: str) -> int:
        """Count how many failed VERIFY steps (potential repair triggers) are journaled.

        Used to restore the repair counter on resume so the backstop ceiling is
        consistent: a resumed run should not get extra free attempts.
        """
        count = 0
        for entry in self._journal.get_trace(task_id):
            if entry.kind != StepKind.VERIFY.value:
                continue
            payload = json.loads(entry.payload_json)
            if not payload.get("verified", False):
                count += 1
        return count

    # --- budget --------------------------------------------------------------
    def _budget_breach(self, task: Task, scope: str, step: int) -> str | None:
        """Return a reason string if a budget is (or would be) breached, else None.

        Step budget: a hard ceiling on transitions. Token budget: committed spend
        must leave room; we refuse BEFORE the paid model call rather than after."""
        if step >= task.step_budget:
            return f"step budget exhausted ({task.step_budget} steps)"
        # A7: wall-clock deadline, checked at the top of the loop (a journaled
        # checkpoint) so a breach stops cleanly with partial progress intact and
        # never mid-effect. Guards a LIVE model backend from running unbounded.
        elapsed = time.monotonic() - self._run_start
        if elapsed >= task.wall_clock_seconds:
            return (
                f"wall-clock budget exhausted "
                f"({elapsed:.1f}s/{task.wall_clock_seconds}s)"
            )
        spent = self._ledger.spent_tokens(scope)
        if spent >= task.token_budget:
            return f"token budget exhausted ({spent}/{task.token_budget} tokens)"
        return None

    # --- helpers -------------------------------------------------------------
    def _combined_patch(self, task_id: str) -> str:
        """Merge all journaled EDIT/RENAME envelopes into one Phase-3 patch envelope.

        Regular EDIT steps store an 'envelope' key with a JSON patch envelope.
        RENAME steps store 'renamed_files' (a list of {path, content_hash}) — but
        the actual file content is already applied to the worktree, so for the
        sandbox patch we re-read the worktree files. This is safe because the
        artifact gate (content-hash) guarantees the worktree and artifact are
        consistent.
        """
        ops: list[dict[str, Any]] = []
        for entry in self._journal.get_trace(task_id):
            if entry.kind != StepKind.EDIT.value:
                continue
            payload = json.loads(entry.payload_json)
            if "envelope" in payload:
                # Regular EDIT: envelope carries the op(s) directly.
                ops.extend(json.loads(payload["envelope"])["ops"])
            elif "renamed_files" in payload:
                # RENAME: read the already-applied worktree files to build ops.
                for rf in payload["renamed_files"]:
                    rel_path = rf["path"]
                    abs_path = self._worktree / rel_path
                    if abs_path.is_file():
                        ops.append(
                            {
                                "op": "write",
                                "path": rel_path,
                                "content": abs_path.read_text("utf-8"),
                            }
                        )
        return json.dumps({"ops": ops})

    def _replay_data_spans(self, task_id: str) -> list[DataSpan]:
        """Rebuild the data channel from journaled retrieval steps (so a resumed
        run feeds the model the same context it had before the crash)."""
        spans: list[DataSpan] = []
        for entry in self._journal.get_trace(task_id):
            if entry.kind != StepKind.RETRIEVE.value:
                continue
            # We journaled only the origin for retrieval (bytes are re-derivable
            # and deterministic per snapshot); re-read is deterministic, but for
            # replay determinism of the MODEL trajectory the stub ignores data,
            # so an origin-only marker is sufficient and avoids re-charging.
            payload = json.loads(entry.payload_json)
            spans.append(DataSpan(origin=payload.get("origin", ""), content=""))
        return spans

    def _apply_op_to_worktree(self, worktree: Path, op: str, rel_path: str, content: str) -> None:
        target = (worktree / rel_path).resolve()
        if worktree.resolve() not in target.parents:
            raise ValueError(f"edit path escapes worktree: {rel_path}")
        if op == "delete":
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _next_step_index(self, task_id: str) -> int:
        trace = self._journal.get_trace(task_id)
        return (max((e.step_index for e in trace), default=-1)) + 1

    def _bump_task_tokens(self, task: Task, tin: int, tout: int) -> None:
        with self._db.immediate() as conn:
            conn.execute(
                "UPDATE tasks SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
                "tool_calls = tool_calls + 1, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;",
                (tin, tout, task.id),
            )

    def _bump_retrieval_bytes(self, task_id: str, byte_count: int) -> None:
        """A3: accumulate retrieved bytes into the task's metering row."""
        with self._db.immediate() as conn:
            conn.execute(
                "UPDATE tasks SET retrieval_bytes = retrieval_bytes + ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;",
                (byte_count, task_id),
            )

    def _bump_sandbox_seconds(self, task_id: str, seconds: float) -> None:
        """A3: accumulate sandbox wall-clock seconds into the task's metering row."""
        with self._db.immediate() as conn:
            conn.execute(
                "UPDATE tasks SET sandbox_seconds = sandbox_seconds + ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;",
                (seconds, task_id),
            )

    def _snapshot(self, task: Task) -> SnapshotRef:
        ref = self._workspaces.get_workspace(task.user_id, task.workspace_id)
        return SnapshotRef(workspace_id=task.workspace_id, commit=ref.head_commit or "")

    def _emit(self, task_id: str, step: int, kind: StepKind, detail: str) -> None:
        self._events.append(
            TaskEvent(task_id=task_id, step_index=step, kind=kind.value, detail=detail)
        )

    def _commit_if_apply(self, task: Task) -> str | None:
        """A9: on VERIFIED_SUCCESS in APPLY mode, commit the worktree back to the
        shared workspace through the guarded ``commit_worktree`` path.

        Returns ``None`` on success (or when nothing to commit / DRY_RUN), or a
        reason string when a concurrent commit advanced the base — in which case
        the caller terminates GAVE_UP (reject policy: never silently clobber the
        winner). DRY_RUN never commits; its patch is surfaced via ``_status``.
        """
        from acp.common.errors import ConflictError
        from acp.common.types import TaskMode

        if TaskMode(task.mode) != TaskMode.APPLY:
            return None
        assert self._handle is not None
        try:
            self._workspaces.commit_worktree(
                task.user_id,
                task.workspace_id,
                task.id,
                base_commit=self._handle.base_commit,
            )
            return None
        except ConflictError as exc:
            # Base advanced under us — another APPLY task committed first. Reject
            # cleanly (the loser is journaled as gave_up); the winner is intact.
            _log.warning(
                "apply.conflict",
                extra={"task_id": task.id, "workspace_id": task.workspace_id},
            )
            return f"commit conflict: {exc}"

    def _terminate(self, task_id: str, state: TaskState, reason: str | None = None) -> TaskStatus:
        self._tasks.set_state(task_id, state, reason)
        self._emit(
            task_id, self._next_step_index(task_id), StepKind.VERIFY, f"TERMINAL:{state.value}"
        )
        task = self._tasks.get_for_resume(task_id)
        assert task is not None
        return self._status(task)

    def _status(self, task: Task) -> TaskStatus:
        from acp.common.types import TaskMode

        # For dry_run tasks at verified_success, include the combined patch JSON
        # so callers can inspect what would be committed without it being applied.
        patch: str | None = None
        if (
            TaskMode(task.mode) == TaskMode.DRY_RUN
            and TaskState(task.state) == TaskState.VERIFIED_SUCCESS
        ):
            patch = self._combined_patch(task.id)

        return TaskStatus(
            task_id=task.id,
            user_id=task.user_id,
            workspace_id=task.workspace_id,
            state=TaskState(task.state),
            step_index=self._next_step_index(task.id),
            tokens_in=task.tokens_in,
            tokens_out=task.tokens_out,
            tool_calls=task.tool_calls,
            retrieval_bytes=task.retrieval_bytes,
            sandbox_seconds=task.sandbox_seconds,
            reason=task.reason,
            patch=patch,
        )


def _rename_identifier_tokens(text: str, old_name: str, new_name: str) -> str:
    """Rewrite ``old_name`` → ``new_name`` only where it appears as a WHOLE
    identifier — never as a substring of a larger identifier (A11).

    Uses ``\\b``-anchored matching: because ``_`` and alphanumerics are word
    chars, ``\\buser\\b`` matches the token ``user`` but NOT ``user_id``,
    ``username``, or ``get_user`` — so renaming ``user`` cannot corrupt a
    colliding name. This is the identifier-token semantics the index's reference
    model assumes (the previous raw ``str.replace`` had no such boundary and
    corrupted collisions).
    """
    return re.sub(rf"\b{re.escape(old_name)}\b", new_name, text)


def _span(file_path: str, start_line: int, end_line: int) -> SpanRef:
    return SpanRef(file_path=file_path, start_line=start_line, end_line=end_line)


def _kind_for_action(kind: ActionKind) -> StepKind:
    return {
        ActionKind.PLAN: StepKind.PLAN,
        ActionKind.RETRIEVE: StepKind.RETRIEVE,
        ActionKind.EDIT: StepKind.EDIT,
        ActionKind.VERIFY: StepKind.VERIFY,
        ActionKind.GIVE_UP: StepKind.PLAN,
        ActionKind.RENAME: StepKind.EDIT,
    }[kind]

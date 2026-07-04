"""Deterministic, keyless stub model backend (Phase 4 + Phase 5).

This is what the eval runs against — no API key, no network, fully reproducible.
Its whole job is to be a *deterministic function of the labeled channels*, so
the agent loop's journal is replayable and the trust-boundary oracle is decidable.

Two properties are load-bearing:

* **Deterministic given (inputs).** ``complete()`` returns the same XML and the
  same token counts for the same ``ModelRequest``. Token counts are a pure
  function of the rendered prompt/response bytes (no randomness, no clock), so
  the ledger totals replay identically — the basis for Phase-4 no-double-charge.

* **Control flow reads ONLY the instruction channel.** The stub decides its next
  action from ``request.instruction_xml`` and ``request.step_index`` alone. It
  never inspects ``request.data_xml``. That is the concrete mechanism behind the
  differential injection test: the planted instruction lives in the data channel,
  the stub cannot see it, so a run WITH the injection produces a byte-identical
  action sequence to a run WITHOUT it. Divergence would mean the boundary leaked;
  by construction it cannot.

The stub emits actions from the strict allow-listed vocabulary in
:mod:`acp.model_gateway.prompt`. The scripted trajectory for the single-file
task is: plan → retrieve the target definition → edit (propose a self-contained
passing test as a span/patch write) → verify. The loop, not the stub, decides
what to do with a verify verdict.

**Phase-5 self-repair trajectory (``fail_first=1``):**
When the instruction carries ``fail_first=1``, the stub deliberately proposes a
failing edit on step 2, then — after the loop feeds the real failure output via
the data channel — proposes the passing corrected edit on step 4 (after the loop
has advanced the step counter past the failed VERIFY + repair-enter). The trajectory
becomes: plan → retrieve → edit(BAD) → verify(fails) → edit(GOOD) → verify(passes).
This drives the oracle: the test asserts that the repair input contained the ACTUAL
Phase-3 captured error text, delivered via the data channel.
"""

from __future__ import annotations

import re

from acp.model_gateway.interface import ModelRequest, ModelResponse
from acp.model_gateway.prompt import ActionKind, AgentAction, render_action

# Extract the target symbol the *instruction* names (trusted channel only). The
# loop puts a "target_symbol=<name>" hint in the instruction when it knows one;
# absent that we fall back to a stable default so the stub is still total.
_TARGET_RE = re.compile(r"target_symbol=([A-Za-z_][A-Za-z0-9_]*)")
_DEFAULT_TARGET = "serialize_user"

# Detects the fail_first=1 hint in the instruction channel. When present the
# stub proposes a bad edit on step 2 (to drive the repair cycle) then a good
# one on step 4. Control flow reads ONLY the instruction channel — never data.
_FAIL_FIRST_RE = re.compile(r"\bfail_first=1\b")

# Detects rename_target and new_name hints in the instruction channel.
# When present the stub emits: PLAN → RENAME(old, new) → VERIFY.
_RENAME_TARGET_RE = re.compile(r"\brename_target=([A-Za-z_][A-Za-z0-9_]*)\b")
_NEW_NAME_RE = re.compile(r"\bnew_name=([A-Za-z_][A-Za-z0-9_]*)\b")

# The patch body the stub proposes: a new, self-contained passing test against
# the target symbol. Written as a span/patch WRITE op (Phase-3 envelope shape).
# Deterministic text ⇒ deterministic content-hash ⇒ apply-exactly-once on resume.
_TEST_PATH = "backend/tests/test_agent_change.py"


def _proposed_bad_edit_content(target: str) -> str:
    """A deliberately wrong edit written to a non-canonical path.

    The FakeSandbox checks for _TEST_PATH; this lands at a different path, so
    the sandbox will return verified=False — driving the self-repair cycle.
    The content itself is syntactically valid so it is not a build failure; it
    is a test-coverage failure (wrong file) which the repair must correct.
    """
    return (
        f'"""Bad first attempt for {target} — wrong path, repair needed."""\n\n'
        "# This file was written to the wrong location by the stub's first attempt.\n"
        f"def test_bad_attempt_{target}() -> None:\n"
        "    pass  # not the file the FakeSandbox looks for\n"
    )


def _proposed_edit_content(target: str) -> str:
    return (
        f'"""Agent-authored verification test for {target} (Phase-4 stub)."""\n\n'
        "from __future__ import annotations\n\n"
        "from app.users.models import User\n"
        f"from app.users.service import {target}\n\n\n"
        "def test_agent_change_serialize_active() -> None:\n"
        '    user = User(id="a1", name="Ada Lovelace", email="ada@example.com")\n'
        f"    result = {target}(user)\n"
        '    assert result["id"] == "a1"\n'
        '    assert result["active"] is True\n'
    )


class StubModelBackend:
    """Deterministic keyless :class:`acp.model_gateway.interface.ModelBackend`.

    Keyless by construction — it never reads an API key or opens a socket.
    """

    name = "stub"

    def complete(self, request: ModelRequest) -> ModelResponse:
        target = self._target(request.instruction_xml)
        fail_first = _FAIL_FIRST_RE.search(request.instruction_xml) is not None
        rename_target = self._rename_target(request.instruction_xml)
        new_name = self._new_name(request.instruction_xml)
        action = self._action_for_step(
            request.step_index,
            target,
            fail_first=fail_first,
            rename_target=rename_target,
            new_name=new_name,
        )
        content_xml = render_action(action)
        # Deterministic usage: a pure function of the bytes in/out. No clock,
        # no RNG, no tokenizer that could vary across machines. This is what
        # makes the ledger totals identical on replay.
        input_tokens = _token_estimate(request.instruction_xml + request.data_xml)
        output_tokens = _token_estimate(content_xml)
        return ModelResponse(
            content_xml=content_xml,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            backend=self.name,
            cached=False,
        )

    # --- deterministic trajectory (instruction channel only) ----------------
    def _action_for_step(
        self,
        step_index: int,
        target: str,
        *,
        fail_first: bool = False,
        rename_target: str | None = None,
        new_name: str | None = None,
    ) -> AgentAction:
        """Map the loop's logical step to the next action.

        The loop calls the model once per non-terminal transition and passes the
        step index. Steps beyond the scripted trajectory repeat VERIFY, so the
        stub is total for any step count without ever inventing a new verb.

        **fail_first trajectory (Phase-5 self-repair oracle):**
        step 0: PLAN
        step 1: RETRIEVE
        step 2: EDIT (bad — the FakeSandbox won't find the expected file)
        step 3: VERIFY (loop runs sandbox → fails → feeds failure to data channel,
                        increments step counter; loop calls model at step 4)
        step 4: EDIT (good — the corrected patch the FakeSandbox accepts)
        step 5+: VERIFY (passes → verified_success)

        The loop decides step 3 internally: when sandbox fails it feeds the
        failure span into data_spans and loops to step 4 calling the model.
        """
        # --- rename trajectory: PLAN → RENAME → VERIFY -----------------------
        if rename_target and new_name:
            if step_index <= 0:
                return AgentAction(
                    ActionKind.PLAN,
                    {
                        "summary": f"rename {rename_target} to {new_name} across all call sites",
                        "target_symbol": rename_target,
                    },
                )
            if step_index == 1:
                return AgentAction(
                    ActionKind.RENAME,
                    {"old_name": rename_target, "new_name": new_name},
                )
            return AgentAction(ActionKind.VERIFY)

        if step_index <= 0:
            return AgentAction(
                ActionKind.PLAN,
                {"summary": f"add a passing test exercising {target}", "target_symbol": target},
            )
        if step_index == 1:
            return AgentAction(
                ActionKind.RETRIEVE, {"primitive": "definition", "name": target}
            )
        if step_index == 2:
            if fail_first:
                # Propose a deliberately wrong path — the FakeSandbox checks for
                # _TEST_PATH; this one will NOT be found, so verified=False.
                return AgentAction(
                    ActionKind.EDIT,
                    {
                        "op": "write",
                        "file_path": "backend/tests/test_bad_attempt.py",
                        "content": _proposed_bad_edit_content(target),
                    },
                )
            return AgentAction(
                ActionKind.EDIT,
                {"op": "write", "file_path": _TEST_PATH, "content": _proposed_edit_content(target)},
            )
        if step_index == 4 and fail_first:
            # Corrected repair edit — write the path the FakeSandbox expects.
            return AgentAction(
                ActionKind.EDIT,
                {"op": "write", "file_path": _TEST_PATH, "content": _proposed_edit_content(target)},
            )
        return AgentAction(ActionKind.VERIFY)

    @staticmethod
    def _target(instruction_xml: str) -> str:
        m = _TARGET_RE.search(instruction_xml)
        return m.group(1) if m else _DEFAULT_TARGET

    @staticmethod
    def _rename_target(instruction_xml: str) -> str | None:
        m = _RENAME_TARGET_RE.search(instruction_xml)
        return m.group(1) if m else None

    @staticmethod
    def _new_name(instruction_xml: str) -> str | None:
        m = _NEW_NAME_RE.search(instruction_xml)
        return m.group(1) if m else None


def _token_estimate(text: str) -> int:
    """Integer, deterministic token estimate (~4 bytes/token, +1 overhead).

    Mirrors the retrieval accounting rule so metering is consistent across the
    system, and — crucially — is a pure function of the text, so a replayed
    call would compute the same number (though on replay we reuse the cached
    ledger entry rather than recompute a charge)."""
    return 1 + (len(text.encode("utf-8")) + 3) // 4

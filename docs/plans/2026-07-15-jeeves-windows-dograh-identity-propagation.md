# Jeeves Windows Dograh Identity Propagation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve stable Pipecat tool-call identity through Dograh's HTTP-tool transport, deploy the backward-compatible change safely, and create a new published `Jeeves Windows Voice` agent whose backend and tools are independent of Jeeves Deck.

**Architecture:** Dograh remains the only shared platform layer. Its HTTP executor carries reserved, model-non-overridable runtime identity into opted-in HTTP requests. The new Dograh workflow owns independent Windows tool records that target only the Jeeves Windows gateway. The gateway's allowlist and stable identity checks remain the authorization/deduplication boundary. The existing Deck pilot is protected state and must be byte-for-byte/configuration-equivalent before and after deployment.

**Tech Stack:** Python 3.13, FastAPI, Pipecat (pinned submodule), httpx, pytest/pytest-asyncio, Docker Compose, Jeeves Windows TypeScript gateway tests.

---

## Fixed paths and boundaries

- Dograh worktree: `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation`
- Dograh branch: `feature/jeeves-windows-identity-propagation`
- Jeeves Windows worktree: `/home/mmounier/.openclaw/workspace/apps/jeeves-windows/.worktrees/dograh-voice-connector`
- Existing Deck workflow: `Jeeves Deck Tool Bridge Pilot` — inspect and verify only
- New workflow: `Jeeves Windows Voice` — create with new IDs and Windows-owned records
- No push, merge, rebase, GPU-laptop installation, real Windows action, Checkpoint 4 work, endpoint/certificate/provider-secret changes, or Deck object mutation.
- Do not delegate further. Stop on active-call ambiguity, protected-state drift, missing rollback proof, or any need to expand scope.

## Task 1: Freeze source and live-state baselines

**Files:**

- Create: `docs/qualification/jeeves-windows-identity-propagation-ledger.md`

**Steps:**

1. Record Dograh branch, base SHA, pinned Pipecat SHA, clean/known worktree status, current deployment source/image identity, service inventory, and the exact rollback command without recording secrets.
2. Inventory the Deck pilot by exact workflow/tool/token IDs and redacted exports. Hash canonicalized exports and record the hashes in the ledger.
3. Inventory active calls using the deployed runtime's authoritative API/database mechanism. If zero active calls cannot be proven, stop before any restart.
4. Inventory the existing Jeeves Windows candidate and gateway SHA. Preserve the untracked `.openclaw/` review artifacts.
5. Commit the ledger before code changes.

```bash
git status --short
git rev-parse HEAD
git submodule status
```

Expected: Dograh starts from the committed design/plan branch with no unexplained tracked changes; protected IDs and rollback facts are recorded.

Commit: `docs: record Dograh identity propagation baseline`

## Task 2: Add failing executor-level identity tests

**Files:**

- Modify: `api/tests/test_custom_tools.py`
- Test: `api/tests/test_custom_tools.py`

**Steps:**

1. Add red tests for an opted-in HTTP tool proving a reserved runtime envelope is delivered for POST/PUT/PATCH without changing model arguments.
2. Add red tests for GET/DELETE proving the same identity is delivered in the selected method-safe wire location.
3. Prove reserved values win when model arguments or preset parameters attempt to supply the same reserved names.
4. Prove missing runtime identity fails closed for an opted-in tool.
5. Prove a legacy/non-opted-in tool produces the exact pre-change request shape.
6. Run only the new tests and capture the expected failures.

```bash
PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:5432/dograh_test REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/python -m pytest api/tests/test_custom_tools.py -q
```

Expected before implementation: only the new identity assertions fail.

## Task 3: Implement the minimal executor transport

**Files:**

- Modify: `api/services/workflow/tools/custom_tool.py`
- Modify: `api/tests/test_custom_tools.py`

**Steps:**

1. Introduce a typed runtime-identity value accepted by `execute_http_tool`.
2. Add a single explicit tool-config opt-in for forwarding reserved identity metadata.
3. Build the reserved envelope after resolving model and preset arguments so neither can overwrite it.
4. Keep credentials and other sensitive context out of the envelope and logs.
5. For an opted-in tool, return a structured error before I/O when required stable identity is absent.
6. Keep non-opted-in requests byte-for-byte equivalent at the mocked `httpx` boundary.
7. Run the focused tests until green.

Commit: `feat: support reserved HTTP tool runtime identity`

## Task 4: Add failing handler-level propagation tests

**Files:**

- Modify: `api/tests/test_custom_tools.py`
- Test: `api/tests/test_custom_tools.py`

**Steps:**

1. Extend the registered-handler test so `FunctionCallParams.tool_call_id` is mandatory and forwarded unchanged.
2. Prove the engine's `_workflow_run_id` is forwarded as Dograh run identity.
3. Invoke the same handler twice with the same logical `tool_call_id` and prove the forwarded identity is stable.
4. Invoke different tool calls and prove identities do not collide.
5. Prove result-callback behavior is unchanged.
6. Run the new tests and capture the expected failures.

## Task 5: Propagate identity from Pipecat to the executor

**Files:**

- Modify: `api/services/workflow/pipecat_engine_custom_tools.py`
- Modify: `api/services/workflow/tools/custom_tool.py`
- Modify: `api/tests/test_custom_tools.py`

**Steps:**

1. Construct runtime identity inside `_create_http_tool_handler` from `function_call_params.tool_call_id`, the engine workflow-run ID, tool UUID, and an explicit Dograh agent scope supplied by tool configuration.
2. Pass that immutable value to `execute_http_tool`.
3. Validate stable ID format only to the extent required by Dograh; preserve the upstream value rather than synthesizing a retry-unstable replacement.
4. Redact identity appropriately in logs while retaining enough correlation for receipts.
5. Run all 46+ focused tests.

Commit: `feat: propagate Pipecat tool call identity to HTTP tools`

## Task 6: Run Dograh code-quality and regression gates

**Files:**

- Modify only if a test exposes an in-scope defect.

**Steps:**

1. Run `git diff --check`.
2. Run `./scripts/format.sh --check` if supported; otherwise use the repo-documented non-mutating formatter check.
3. Run `./scripts/lint.sh` and the API mypy command documented by the repository.
4. Run the full API test suite with the pinned Pipecat submodule and required test-only dependencies.
5. Re-run `api/tests/test_custom_tools.py` separately and record exact counts.
6. Commit only necessary focused corrections.

Expected: all relevant gates green; no unexplained snapshot or migration changes.

Commit if needed: `test: cover Dograh HTTP tool identity propagation`

## Task 7: Reverify the Jeeves Windows gateway contract

**Files:**

- Modify only if the existing documented wire contract differs from the final Dograh envelope.
- Update: `/home/mmounier/.openclaw/workspace/apps/jeeves-windows/.worktrees/dograh-voice-connector/docs/qualification/dograh-voice-capability-verification.md`

**Steps:**

1. Compare the final Dograh wire identity with the gateway's strict `dgt_` stable-ID admission rule and workflow/tool allowlist.
2. Add or update tests for valid identity, missing identity, wrong scope, unknown tool, repeated identity, and model override attempts.
3. Run gateway lint, typecheck, all tests, and the 1,000-iteration stress test using commands already documented in the Checkpoint 3 handback.
4. Commit any required gateway contract/test adjustment separately in the Jeeves Windows branch.

Expected: the gateway fails closed and performs at most one side effect for a repeated stable identity.

## Task 8: Build an immutable rollback-tagged Dograh image

**Files:**

- Update: `docs/qualification/jeeves-windows-identity-propagation-ledger.md`

**Steps:**

1. Verify Dograh tracked state is committed and record the exact SHA.
2. Build through the deployment's existing documented Compose/image path; do not change ports, endpoints, certificates, or provider credentials.
3. Tag the candidate with the Dograh SHA and record its immutable image digest.
4. Prove the previous image remains locally/addressably available for rollback.
5. Run container health and focused synthetic HTTP-tool tests without touching the protected Deck workflow.

Expected: candidate and rollback digests are both recorded before deployment.

## Task 9: Controlled shared-runtime deployment

**Files:**

- Update: `docs/qualification/jeeves-windows-identity-propagation-ledger.md`

**Steps:**

1. Re-check zero active calls immediately before mutation.
2. Re-export/hash the Deck pilot and compare with the Task 1 baseline.
3. Deploy/restart only the minimum Dograh API service using the existing deployment procedure.
4. Verify service health, port-3010 UI/API availability, database/Redis connectivity, and current image digest.
5. Re-export/hash the Deck pilot and run a non-mutating Deck smoke test.
6. Roll back immediately if any health check, hash, exact-ID inventory, or Deck smoke result differs unexpectedly.

Expected: candidate image healthy; Deck state and behavior unchanged; rollback remains ready.

## Task 10: Prove deployed identity semantics against fake Windows

**Files:**

- Update: `docs/qualification/jeeves-windows-identity-propagation-ledger.md`
- Update: `/home/mmounier/.openclaw/workspace/apps/jeeves-windows/.worktrees/dograh-voice-connector/docs/qualification/dograh-voice-capability-verification.md`

**Steps:**

1. Create only exact-ID-ledgered, Windows-owned staging objects and HTTP tools; never modify a Deck-owned record.
2. Point them only at fake Windows/gateway fixtures.
3. Run text and voice probes proving stable identity, retry deduplication, timeout, cancellation, malformed identity rejection, wrong-scope rejection, and unknown-tool rejection.
4. Prove the same logical retry retains identity and causes at most one fake side effect.
5. Clean up only disposable objects created in this task by their recorded IDs. Do not remove the final dedicated workflow created next.

Expected: D0 is cleared with deployed evidence and D2/R1 can proceed.

## Task 11: Create and publish the dedicated Windows agent

**Files:**

- Create: `/home/mmounier/.openclaw/workspace/apps/jeeves-windows/.worktrees/dograh-voice-connector/docs/qualification/dograh-jeeves-windows-integration-record.md`

**Steps:**

1. Create a new workflow named `Jeeves Windows Voice` with a new workflow ID, publication object, token/session configuration, and prompt.
2. Create new Windows-owned tool records only. Use a distinct naming namespace and explicit immutable `agent_scope=jeeves_windows` metadata.
3. Treat the Deck capability inventory as requirements only. Attach no Deck object and route no request through the Deck backend.
4. Attach only Windows capabilities that have an independent gateway/broker/app implementation; record unsupported Deck-only capabilities as unavailable.
5. Run positive routing tests and negative ambiguous-utterance tests against fake Windows.
6. Publish the agent and prove it remains callable through a synthetic session.
7. Record all exact IDs, redacted configuration hashes, Dograh SHA/image digest, tool manifest, and explicit no-Deck-dependency statement in the integration record.

Expected: `Jeeves Windows Voice` persists published/callable and is operationally independent of Deck.

Commit in Jeeves Windows: `docs: record dedicated Dograh Windows voice integration`

## Task 12: Final verification and handback

**Files:**

- Update both qualification records.
- Create the Checkpoint 3 return handback in the established workspace handback location.

**Steps:**

1. Re-run Dograh focused/full gates and Jeeves Windows gateway/deployment gates.
2. Re-export/hash the Deck pilot a final time and compare to Task 1.
3. Confirm both worktrees have only expected committed changes; preserve review artifacts.
4. Report exact SHAs, image digests, workflow/tool IDs, test counts, deployment receipts, rollback readiness, and residual risks.
5. Stop before installing or invoking the GPU-laptop application. Provide the prerequisites and attended test procedure for parent review, not as an executed action.

Expected handback state: all automated and fake-backed gates green; new Windows agent published/callable; Deck unchanged; real GPU-laptop call awaiting Maxim's attended acceptance.

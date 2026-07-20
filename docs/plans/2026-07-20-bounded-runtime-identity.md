# Bounded HTTP Tool Runtime Identity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Canonicalize arbitrary provider tool-call IDs into stable bounded Dograh runtime identities so Spotify can be opened reliably on the main GPU laptop.

**Architecture:** Dograh hashes each nonempty provider tool-call ID into `tcid:v1:<sha256>` before constructing the reserved HTTP identity header. The gateway and Windows connector remain unchanged: the gateway continues strict envelope validation and derives replay/idempotency identity from the bounded value.

**Tech Stack:** Python 3, pytest, Loguru, HTTPX, Node.js/TypeScript gateway tests, Docker Compose.

---

### Task 1: Add the canonical identity contract test-first

**Files:**
- Modify: `api/services/workflow/tools/custom_tool.py:1-50`
- Modify: `api/services/workflow/pipecat_engine_custom_tools.py:373-410`
- Test: `api/tests/test_custom_tools.py:1408-1495`

**Step 1: Write the failing canonicalization tests**

Add focused tests that pass the observed provider-ID lengths through the real registered handler and inspect the `HttpToolRuntimeIdentity` sent to `execute_http_tool`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("length", [700, 1268])
async def test_http_handler_canonicalizes_long_provider_tool_call_id(self, length):
    manager, tool = self._identity_manager_and_tool()
    handler = manager._create_http_tool_handler(tool, "windows_open_app")
    raw_id = "x" * length
    params = Mock(
        tool_call_id=raw_id,
        arguments={"app_id": "app:v1:spotify"},
        result_callback=AsyncMock(),
    )

    with patch(
        "api.services.workflow.pipecat_engine_custom_tools.execute_http_tool",
        new_callable=AsyncMock,
        return_value={"status": "success"},
    ) as mock_execute:
        await handler(params)

    identity = mock_execute.await_args.kwargs["runtime_identity"]
    assert identity.tool_call_id == (
        "tcid:v1:" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
    )
    assert len(identity.tool_call_id) == 72
```

Update the retry and distinct-call tests to assert canonical IDs remain equal for retries and unequal for distinct raw IDs. Update the forwarding test to assert the raw provider ID is absent from the serialized header.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:5432/dograh_test REDIS_URL=redis://127.0.0.1:6379/15 \
  .venv/bin/python -m pytest \
  api/tests/test_custom_tools.py::TestCustomToolManagerUnit::test_http_handler_canonicalizes_long_provider_tool_call_id \
  -q
```

Expected: FAIL because the handler still forwards the 700- and 1,268-character raw IDs.

**Step 3: Implement the minimal canonicalizer**

In `custom_tool.py`, add:

```python
import hashlib

RUNTIME_TOOL_CALL_ID_PREFIX = "tcid:v1:"


def canonicalize_http_tool_call_id(provider_tool_call_id: str) -> str:
    """Return a stable bounded identity for one provider-owned tool call."""
    if not isinstance(provider_tool_call_id, str) or not provider_tool_call_id.strip():
        raise ValueError("Stable tool call identity is required for this HTTP tool")
    digest = hashlib.sha256(provider_tool_call_id.encode("utf-8")).hexdigest()
    return f"{RUNTIME_TOOL_CALL_ID_PREFIX}{digest}"
```

Import the helper in `pipecat_engine_custom_tools.py`. Preserve the existing missing-ID terminal result, then construct `HttpToolRuntimeIdentity` with the canonical value. Keep correlation logging derived from the raw ID without logging it.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:5432/dograh_test REDIS_URL=redis://127.0.0.1:6379/15 \
  .venv/bin/python -m pytest api/tests/test_custom_tools.py -q
```

Expected: all custom-tool tests pass.

**Step 5: Commit the source correction**

```bash
git add api/services/workflow/tools/custom_tool.py \
  api/services/workflow/pipecat_engine_custom_tools.py \
  api/tests/test_custom_tools.py
git commit -m "fix: bound HTTP tool runtime identities"
```

### Task 2: Prove compatibility and regression safety

**Files:**
- Test only: `api/tests/test_custom_tools.py`
- Test only: `/home/mmounier/.openclaw/workspace/apps/jeeves-windows/.worktrees/dograh-voice-connector/apps/jeeves-voice-gateway/test/toolface.test.ts`

**Step 1: Run adjacent Dograh identity/security suites**

Run:

```bash
PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:5432/dograh_test REDIS_URL=redis://127.0.0.1:6379/15 \
  .venv/bin/python -m pytest \
  api/tests/test_custom_tools.py \
  api/tests/test_mcp_tool_creation.py \
  api/tests/test_mcp_tool_route.py \
  -q
```

Expected: all selected tests pass.

**Step 2: Run the broad Dograh API suite**

Run:

```bash
PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:5432/dograh_test REDIS_URL=redis://127.0.0.1:6379/15 \
  .venv/bin/python -m pytest api/tests -q
```

Expected: no new failures relative to the documented baseline. Any baseline failures must be reproduced at pre-change commit `f82552b` before being classified as unrelated.

**Step 3: Run the unchanged gateway contract suite**

From the Jeeves Windows worktree:

```bash
npm --prefix apps/jeeves-voice-gateway test -- --runInBand
```

Expected: gateway tests pass; the existing strict bound accepts the 72-character canonical value without source changes.

**Step 4: Check repository hygiene**

Run in both worktrees:

```bash
git diff --check
git status --short
```

Expected: only known operator-owned `.openclaw/` and `state/` paths remain untracked; no unrelated changes.

### Task 3: Build and stage the Dograh API rollout

**Files:**
- No source files beyond Task 1
- Create: `docs/qualification/2026-07-20-bounded-runtime-identity.md`

**Step 1: Confirm the live gate**

Verify no workflow run is active, the API and gateway are healthy, and the GPU connector owner is connected. Stop if call state is active or unknown.

**Step 2: Preserve rollback**

Record the current API image ID and tag it with a unique rollback tag. Capture the exact two Compose files and relevant redacted runtime configuration. Do not modify the database or tool membership.

**Step 3: Build the candidate image from the approved worktree**

Build a commit-addressed API image from this worktree using the same Dockerfile and audio-provider build inputs as the running image. Do not overwrite the rollback tag.

**Step 4: Recreate only the API service**

Use project `dograh-local` and the existing Compose files:

```text
/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/docker-compose.yaml
/home/mmounier/.openclaw/workspace/apps/jeeves-deck/docker/dograh-api-override.yaml
```

Retag the verified candidate as `dograh-local-api:openrouter-audio`, recreate only `api`, and wait for bounded health. Do not restart the gateway, TURN, database, UI, or Windows connector.

**Step 5: Verify live static and synthetic gates**

Confirm:

- API and gateway health are green with zero candidate restarts.
- The live API image matches the candidate digest.
- TURN public/internal split-host values remain unchanged.
- Existing workflow/tool membership is unchanged.
- A synthetic opted-in HTTP-tool execution with an observed-size ID reaches gateway validation using a 72-character canonical identity without exposing the raw ID.

On any failure, restore the rollback API tag and recreate only `api`.

### Task 4: Human GPU acceptance and handback

**Files:**
- Modify: `docs/qualification/2026-07-20-bounded-runtime-identity.md`

**Step 1: Run one controlled voice call**

The operator says: “Find Spotify, then open it.” Dograh must first use `find_app`, then pass the returned opaque `app_id` to `open_app`.

**Step 2: Verify the visible result**

The operator confirms Spotify visibly launches on the main GPU laptop. Playback and login are not tested.

**Step 3: Verify durable truth**

Call `check_last_action` during the same voice session. Confirm the tool result is successful, the gateway journal contains one terminal execution for the mutation, and there is no duplicate execution.

**Step 4: Verify teardown**

End the call manually. Confirm Connector remains `Ready`, Media returns to idle, the microphone indicator releases, and no `invalid_runtime_identity` or connector failure appears.

**Step 5: Record qualification**

Add timestamps, run ID, tool request/result evidence, image digest, rollback path, and the human-visible Spotify result to the qualification document. Commit only after the human acceptance gate is complete.

No push, merge, capability expansion, `end_call`, Spotify playback, or migration to the GPU host belongs to this milestone.

# LAN TURN Split-Host Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Aptivio-hosted Dograh TURN relay usable by the same-LAN GPU laptop while keeping Dograh's container-side TURN route internal and preserving existing installations.

**Architecture:** Keep `TURN_HOST` as the client-visible relay host and add an optional `TURN_INTERNAL_HOST` for server-side aiortc. Allow local coturn rendering to accept an explicit public/private `TURN_EXTERNAL_IP` mapping, then qualify the exact Aptivio deployment with forced relay and LAN-only firewall exposure.

**Tech Stack:** Python 3, FastAPI, aiortc, pytest, Bash, PowerShell, Docker Compose, coturn, WPF WebView2/WebRTC.

---

### Task 1: Separate public and internal TURN URI generation

**Files:**
- Create: `api/tests/test_turn_split_host.py`
- Modify: `api/constants.py`
- Modify: `api/routes/turn_credentials.py`
- Modify: `api/routes/webrtc_signaling.py`

**Step 1: Write the failing credential-host tests**

Add tests that reload the TURN modules with:

```python
monkeypatch.setenv("TURN_HOST", "10.0.0.133")
monkeypatch.setenv("TURN_INTERNAL_HOST", "host.docker.internal")
```

Assert that:

```python
public = generate_turn_credentials("public-user")
internal = generate_turn_credentials("server-user", host=TURN_INTERNAL_HOST)

assert all("10.0.0.133" in uri for uri in public["uris"])
assert all("host.docker.internal" in uri for uri in internal["uris"])
```

Add a fallback case where `TURN_INTERNAL_HOST` is absent and equals `TURN_HOST`.

**Step 2: Run the focused test and verify it fails**

Run:

```bash
source venv/bin/activate
set -a
source api/.env.test
set +a
python -m pytest api/tests/test_turn_split_host.py -v
```

Expected: FAIL because `TURN_INTERNAL_HOST` and the `host` argument do not exist.

**Step 3: Implement the smallest host split**

In `api/constants.py`, define:

```python
TURN_HOST = os.getenv("TURN_HOST", "localhost")
TURN_INTERNAL_HOST = os.getenv("TURN_INTERNAL_HOST") or TURN_HOST
```

Extend `generate_turn_credentials` with a keyword-only host override that defaults to `TURN_HOST`. Build every returned URI from that selected host. In `get_ice_servers`, request credentials with `host=TURN_INTERNAL_HOST`; do not alter the public credential endpoints.

**Step 4: Run the focused tests and verify they pass**

Run the command from Step 2.

Expected: all split-host and fallback tests PASS.

**Step 5: Run adjacent API tests**

Run:

```bash
python -m pytest \
  api/tests/test_turn_split_host.py \
  api/tests/test_public_embed_cors.py \
  api/tests/test_public_signaling_origin.py \
  api/tests/test_sensitive_session_logging.py -v
```

Expected: PASS with no credential or session token in captured logs.

**Step 6: Commit**

```bash
git add api/constants.py api/routes/turn_credentials.py \
  api/routes/webrtc_signaling.py api/tests/test_turn_split_host.py
git commit -m "fix: separate public and internal TURN hosts"
```

### Task 2: Support an explicit coturn NAT mapping

**Files:**
- Create: `scripts/tests/test_turn_split_host.sh`
- Modify: `docker-compose.yaml`
- Modify: `scripts/run_dograh_init.sh`

**Step 1: Write a failing renderer test**

Create a self-contained shell test that renders local TURN configuration into a temporary directory with:

```bash
ENVIRONMENT=local
TURN_HOST=10.0.0.133
TURN_INTERNAL_HOST=host.docker.internal
TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8
TURN_SECRET=test-only-secret
```

Assert the generated file contains exactly:

```text
external-ip=10.0.0.133/172.28.0.8
```

Add a compatibility case with no `TURN_EXTERNAL_IP` and assert it renders `external-ip=$TURN_HOST`.

**Step 2: Run the renderer test and verify it fails**

Run:

```bash
bash scripts/tests/test_turn_split_host.sh
```

Expected: FAIL because local init overwrites the explicit mapping with `TURN_HOST`.

**Step 3: Pass the advanced settings through Compose**

Add these optional variables to `dograh-init`:

```yaml
TURN_INTERNAL_HOST: "${TURN_INTERNAL_HOST:-}"
TURN_EXTERNAL_IP: "${TURN_EXTERNAL_IP:-}"
```

Add `TURN_INTERNAL_HOST` to the API environment. Do not expose either value to the UI container or Cloudflare tunnel.

**Step 4: Preserve explicit local NAT mapping**

Change the local branch of `scripts/run_dograh_init.sh` to use:

```bash
export TURN_EXTERNAL_IP="${TURN_EXTERNAL_IP:-$TURN_HOST}"
```

Keep the production branch pinned to `SERVER_IP` so current remote behavior is unchanged.

**Step 5: Run renderer and Compose validation**

Run:

```bash
bash scripts/tests/test_turn_split_host.sh
docker compose --profile local-turn config -q
```

Expected: both commands exit 0.

**Step 6: Commit**

```bash
git add docker-compose.yaml scripts/run_dograh_init.sh \
  scripts/tests/test_turn_split_host.sh
git commit -m "fix: render local TURN NAT mappings"
```

### Task 3: Keep local setup scripts in parity

**Files:**
- Modify: `scripts/setup_local.sh`
- Modify: `scripts/setup_local.ps1`
- Modify: `scripts/AGENTS.md`
- Modify: `docs/contribution/setup.mdx`

**Step 1: Extend the shell test with setup-output assertions**

Run both setup scripts in their existing noninteractive/testable mode with `ENABLE_COTURN=true`, a client-visible host, an optional internal host, and an optional external mapping. Assert the generated `.env` contains each non-secret key once and never prints the TURN secret.

**Step 2: Run the test and verify it fails**

Run:

```bash
bash scripts/tests/test_turn_split_host.sh
```

Expected: FAIL because the advanced keys are not written.

**Step 3: Add matching Bash and PowerShell behavior**

Treat `TURN_INTERNAL_HOST` and `TURN_EXTERNAL_IP` as optional advanced environment inputs. Validate nonempty supplied values, write them only when present, and describe `TURN_HOST` as client-visible rather than shared by both peers. Keep all defaults backward compatible.

**Step 4: Document the keys and safety boundary**

Update contributor setup and `scripts/AGENTS.md` to explain:

- `TURN_HOST`: browser/client-visible address;
- `TURN_INTERNAL_HOST`: API-container address, defaults to `TURN_HOST`;
- `TURN_EXTERNAL_IP`: optional coturn public/private NAT mapping;
- no router forwarding is required for a same-LAN install.

**Step 5: Run syntax and parity checks**

Run:

```bash
bash -n scripts/setup_local.sh scripts/run_dograh_init.sh scripts/tests/test_turn_split_host.sh
bash scripts/tests/test_turn_split_host.sh
pwsh -NoProfile -Command '$null = [scriptblock]::Create((Get-Content -Raw scripts/setup_local.ps1))'
```

Expected: all commands exit 0.

**Step 6: Commit**

```bash
git add scripts/setup_local.sh scripts/setup_local.ps1 scripts/AGENTS.md \
  scripts/tests/test_turn_split_host.sh docs/contribution/setup.mdx
git commit -m "docs: expose local TURN split-host settings"
```

### Task 4: Run source-level regression verification

**Files:**
- No source changes expected.

**Step 1: Run focused API tests**

```bash
source venv/bin/activate
set -a
source api/.env.test
set +a
python -m pytest \
  api/tests/test_turn_split_host.py \
  api/tests/test_public_embed_cors.py \
  api/tests/test_public_signaling_origin.py \
  api/tests/test_sensitive_session_logging.py -v
```

Expected: PASS.

**Step 2: Run the full non-provider API suite used by this branch**

Run the repository's established non-provider test command and record the exact pass/fail/skip totals. Any unrelated pre-existing failure must be reproduced at the pre-change revision before it can be dispositioned.

**Step 3: Run deployment checks**

```bash
bash scripts/tests/test_turn_split_host.sh
docker compose --profile local-turn config -q
git diff --check HEAD~3..HEAD
```

Expected: all commands exit 0.

**Step 4: Inspect sensitive paths**

Search active source and rendered configuration for token-bearing signaling or TURN credential paths. Expected: only intentional legacy compatibility routes; no new token or password logging.

### Task 5: Stage the Aptivio LAN configuration with rollback

**Files:**
- Modify operationally: `/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/.env`
- Modify operationally: `/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/docker-compose.yaml`
- Capture: a timestamped rollback directory outside Git containing the original files and rendered coturn configuration

**Step 1: Capture immutable pre-change evidence**

Record container image IDs, restart counts, health, current compose rendering hash, generated coturn hash, active workflow/tool hashes, and gateway owner health. Back up only the exact files/configuration being changed.

**Step 2: Apply the approved private-LAN values**

Set:

```dotenv
TURN_HOST=10.0.0.133
TURN_INTERNAL_HOST=host.docker.internal
TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8
FORCE_TURN_RELAY=true
```

Remove the live compose hardcoding that replaces `TURN_HOST` with `host.docker.internal`; use the new internal variable instead. Keep the existing static coturn Docker address and private bridge subnet.

**Step 3: Validate before restart**

Run:

```bash
docker compose --profile local-turn config -q
```

Render coturn configuration in isolation and assert the exact external mapping, secret hash match, relay range, and no unexpected listeners.

Expected: validation passes before any service changes.

**Step 4: Recreate only affected services**

Recreate `dograh-init`, coturn, and the API in dependency order. Do not recreate PostgreSQL, Redis, MinIO, the gateway, Jeeves Deck, or the Windows connector.

**Step 5: Verify service health and rollback on failure**

Require healthy API/coturn/gateway state and zero unexpected restarts. On any failure, restore the backup and recreate the same affected services before continuing.

### Task 6: Qualify the exact GPU forced-relay path

**Files:**
- Create: `docs/qualification/2026-07-19-lan-turn-split-host.md`

**Step 1: Verify credential audience without disclosing credentials**

Mint one short-lived authenticated media session and inspect only URI hosts. Assert the client receives `10.0.0.133` and the server-side ICE configuration uses `host.docker.internal`. Do not print usernames, passwords, bearer tokens, or session tokens.

**Step 2: Verify TURN listeners from the GPU**

Confirm TCP 3478 from the GPU and, where tooling permits, the configured UDP listener/relay range. Keep firewall scope private-LAN only.

**Step 3: Run one attended GPU voice call**

With `FORCE_TURN_RELAY=true`, press **Start Call** once, allow microphone access, speak a deterministic phrase, and wait for spoken audio.

Expected:

- Connector remains Ready;
- Media reaches Connected;
- microphone is active only during the call;
- Dograh logs a relay candidate and successful audio writes;
- the agent produces an audible response.

**Step 4: Verify teardown**

Press **End Call** and confirm the Windows microphone indicator clears, the WebRTC peer closes, and the Dograh pipeline terminates without a timeout.

**Step 5: Record qualification and commit**

Document exact revisions, image IDs, rendered configuration hashes, sanitized log evidence, rollback location, and human-observed audio result.

```bash
git add docs/qualification/2026-07-19-lan-turn-split-host.md
git commit -m "docs: qualify LAN TURN split-host voice path"
```

### Task 7: Prepare independent Claude CLI review

**Files:**
- Create: `docs/agent-handoffs/handoffs/claude/2026-07-19-claude-review-lan-turn-split-host.md`

**Step 1: Prepare a read-only review packet**

List exact Git ranges, operational configuration hashes, tests, qualification evidence, and review questions covering credential audience separation, coturn NAT mapping, LAN exposure, rollback, and secret logging.

**Step 2: Commit the handoff**

```bash
git add docs/agent-handoffs/handoffs/claude/2026-07-19-claude-review-lan-turn-split-host.md
git commit -m "docs: prepare LAN TURN Claude review"
```

**Step 3: Stop for manual Claude CLI review**

Do not invoke OpenRouter or an API model. Provide Maxim the local Claude CLI handoff command and wait for the returned report before declaring rollout complete.

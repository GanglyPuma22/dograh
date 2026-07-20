# Claude Return: LAN TURN Split-Host Implementation

Status: IMPLEMENTATION COMPLETE — awaiting Jeeves review; live rollout and GPU voice qualification NOT performed.

## Handback contract

- For: Jeeves (independent review before live rollout and the gated human GPU voice test)
- Handoff: `docs/agent-handoffs/handoffs/claude/2026-07-19-claude-implement-lan-turn-split-host.md`
- Heartbeat: `state/runs/2026-07-19-claude-lan-turn-split-host.md` (run state, intentionally not committed)
- Nothing was pushed; the live `dograh-local` deployment was not touched, restarted, or reconfigured.

## What was implemented

1. **API host audience separation** — optional `TURN_INTERNAL_HOST` (defaults to
   `TURN_HOST`; empty string treated as absent because compose renders
   `"${TURN_INTERNAL_HOST:-}"`). `generate_turn_credentials` gained a
   keyword-only `host` override; only server-side `get_ice_servers` passes
   `host=TURN_INTERNAL_HOST`. All public/authenticated credential endpoints
   still return `TURN_HOST`. The legacy static-credential fallback
   (`TURN_USERNAME`/`TURN_PASSWORD`) was intentionally left on `TURN_HOST`
   per the approved plan scope (unused when `TURN_SECRET` is set).
2. **coturn NAT-mapping render** — the local branch of
   `scripts/run_dograh_init.sh` now honors an explicit
   `TURN_EXTERNAL_IP` (`public-ip/private-ip`) and still falls back to
   `TURN_HOST`. The production branch stays pinned to `SERVER_IP`
   (regression-tested). `docker-compose.yaml` passes
   `TURN_INTERNAL_HOST`/`TURN_EXTERNAL_IP` to `dograh-init` and
   `TURN_INTERNAL_HOST` to `api` only — nothing new to the UI container or
   Cloudflare tunnel.
3. **Setup-script parity + docs** — `setup_local.{sh,ps1}` accept the two
   optional env inputs (validated, written to `.env` only when non-empty,
   defaults unchanged, no new prompts), describe `TURN_HOST` as
   client-visible, and the split-host keys are documented in
   `docs/deployment/docker.mdx`, `docs/developer/environment-variables.mdx`,
   and `scripts/AGENTS.md`.

### Pre-existing defects found and fixed in-scope (flag for review)

`scripts/setup_local.ps1` could not run at all under Windows PowerShell 5.1
(the interpreter used by the documented `.\setup_local.ps1` flow on a stock
Windows box), for two reasons that predate this branch (introduced in
`ae2efef` on main):

- `"Files created in $CurrentDir:"` is an invalid variable reference on every
  PowerShell version (parse error) — fixed with `${CurrentDir}:`.
- The file was BOM-less UTF-8 with em dashes / box-drawing / check marks;
  PS 5.1 decodes such files as ANSI, turning the em dash into a smart quote
  that terminates a string and breaks parsing — fixed by making the script
  ASCII-only, matching every other `.ps1` in `scripts/`. A convention note
  was added to `scripts/AGENTS.md`.
  (Related: `scripts/start_services_dev.ps1` has 1 non-ASCII line — left
  untouched as out of scope, worth a follow-up.)

These fixes were required for the approved parity test to run the script at
all; without them the PowerShell lane fails on parse, not on behavior.

## Commits (implementation range `ac324a5..a7f1d5b` + return-doc commit)

| SHA | Subject |
| --- | --- |
| `80368a7` | fix: separate public and internal TURN hosts |
| `c82c63f` | fix: render local TURN NAT mappings |
| `a7f1d5b` | docs: expose local TURN split-host settings |
| (see git log) | docs: return LAN TURN split-host implementation |

Files changed: `api/constants.py`, `api/routes/turn_credentials.py`,
`api/routes/webrtc_signaling.py`, `api/tests/test_turn_split_host.py`,
`docker-compose.yaml`, `scripts/run_dograh_init.sh`,
`scripts/tests/test_turn_split_host.sh`, `scripts/setup_local.sh`,
`scripts/setup_local.ps1`, `scripts/AGENTS.md`,
`docs/deployment/docker.mdx`, `docs/developer/environment-variables.mdx`.

## Validation evidence (all fresh, this run)

- **TDD red→green, API** (`api/tests/test_turn_split_host.py`): red run
  4 failed / 2 passed (`AttributeError: module 'api.constants' has no
  attribute 'TURN_INTERNAL_HOST'`; server ICE asserting internal host
  `assert False`) → green 6 passed. Covers: public URIs on `TURN_HOST`,
  internal URIs on the override, absent + empty-string fallback,
  server-side `get_ice_servers` on `TURN_INTERNAL_HOST` with the unchanged
  ephemeral credential scheme, and default single-host ICE behavior.
- **TDD red→green, renderer** (`scripts/tests/test_turn_split_host.sh`):
  red run rendered `external-ip=10.0.0.133` instead of the explicit mapping
  → green renders exactly `external-ip=10.0.0.133/172.28.0.8`; fallback and
  remote `SERVER_IP` pinning cases green.
- **TDD red→green, setup parity**: red run missing `.env` keys and
  accepting an invalid `TURN_EXTERNAL_IP` → green. Final script test:
  22/22 assertions passing, including full runs of `setup_local.ps1` under
  real Windows PowerShell 5.1 (staged in the Windows TEMP dir because PS 5.1
  cannot write from a `\\wsl.localhost` UNC cwd), plus a parse gate and an
  invalid-value rejection case. The `.env` outputs of both scripts are
  asserted line-identical for the TURN keys (CRLF-normalized), and no run
  ever prints the TURN secret.
- **Adjacent security/compat suites**: `test_public_embed_cors.py`,
  `test_public_signaling_origin.py`, `test_sensitive_session_logging.py` +
  the new file: 42 passed, 0 failed.
- **Deployment checks**: `bash -n` on all touched shell scripts OK;
  `docker compose --profile local-turn config -q` exit 0;
  `git diff --check ac324a5..HEAD` clean.
- **Sensitive-path inspection**: token-bearing URL paths exist only on the
  pre-existing legacy compatibility routes
  (`/ws/public/signaling/{session_token}`,
  `/public/embed/turn-credentials/{session_token}`,
  `/public/embed/config/{token}`); the credential-free header/subprotocol
  routes remain primary and redaction tests stay green. A fresh isolated
  coturn render with split-host values contains the secret only in the
  coturn-required `static-auth-secret` line, the exact
  `external-ip=10.0.0.133/172.28.0.8` mapping, and no unexpected listeners.
- **Broad API suite** (CI-equivalent `pytest tests/` from `api/`): at HEAD
  **953 passed, 4 failed** (92s). The identical 4 tests also fail at the
  pre-change revision `ac324a5` (947 passed, 4 failed — the +6 passes are the
  new split-host tests): `test_run_pipeline.py` and
  `test_run_pipeline_text_greeting.py` (`AttributeError:
  'NoopFeedbackObserver' object has no attribute 'emit_media_start'`) and two
  `test_openrouter_audio_provider.py` isinstance/module-identity failures.
  All 4 are pre-existing and unrelated to TURN; dispositioned as such.
- **Final focused rerun at HEAD**: 42 passed, 0 failed.

## Environment deviations (recorded, none affect the product change)

- The plan's canonical `venv/bin/activate` does not exist in this worktree or
  the main checkout. Used the worktree-local `.venv` (Python 3.13.7, full
  API test dependency set) — same interpreter major/minor as CI (3.13).
- `api/.env.test` did not exist. Created it (gitignored) from
  `api/.env.test.example`: the repo-designated `test_db` database (which
  already existed locally; no database was created) and Redis DB index 9
  instead of 0 so test keys cannot touch the live deployment's queue state.
  No test points at the live `postgres` database.
- The plan named `docs/contribution/setup.mdx` for the key documentation,
  but that page is devcontainer-only and contains no TURN content. The
  documentation went to `docs/deployment/docker.mdx` (the page that owns the
  `setup_local` coturn flow) and `docs/developer/environment-variables.mdx`
  instead, per the docs guideline to extend where content already lives.
- PowerShell evidence used Windows PowerShell 5.1 via WSL interop
  (`powershell.exe`); `pwsh` is not installed in WSL. This is the stricter
  interpreter (it exposed the parse bugs above).

## Recommended live-rollout sequence (prepared, NOT executed)

All paths below are the live deployment at
`/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local`.
Values are the Maxim-approved installation-specific ones.

**Phase 0 — capture rollback state (before any change)**
1. Create a timestamped rollback dir outside git, e.g.
   `~/dograh-rollback-$(date +%Y%m%d-%H%M%S)/`.
2. Copy into it: `.env`, `docker-compose.yaml`,
   `scripts/run_dograh_init.sh`, `scripts/lib/setup_common.sh`,
   `deploy/templates/`, and the currently rendered coturn config
   (`docker compose cp coturn:/etc/coturn/turnserver.conf …` or via the
   `coturn-generated` volume).
3. Record `docker compose ps`, image IDs (`docker images --digests` for the
   api/coturn images), and per-container restart counts
   (`docker inspect -f '{{.RestartCount}}' …`).

**Phase 1 — stage files**
1. Refresh the helper bundle from this branch revision (`a7f1d5b`):
   `scripts/run_dograh_init.sh`, `scripts/lib/setup_common.sh`,
   `deploy/templates/*` (only `run_dograh_init.sh` actually changed).
2. Edit the live `docker-compose.yaml`:
   - `dograh-init` environment: add
     `TURN_INTERNAL_HOST: "${TURN_INTERNAL_HOST:-}"` and
     `TURN_EXTERNAL_IP: "${TURN_EXTERNAL_IP:-}"`.
   - `api` environment: replace the hardcoded
     `TURN_HOST: "host.docker.internal"` override with
     `TURN_HOST: "${TURN_HOST:-}"` and add
     `TURN_INTERNAL_HOST: "${TURN_INTERNAL_HOST:-}"`.
   - Keep the live-only extras unchanged: static coturn address
     `172.28.0.8`, the `172.28.0.0/16` subnet, restart policies,
     `extra_hosts: host.docker.internal:host-gateway`, and the API host
     port binding.
3. Set in `.env` (no other keys touched):
   `TURN_HOST=10.0.0.133`, `TURN_INTERNAL_HOST=host.docker.internal`,
   `TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8`, `FORCE_TURN_RELAY=true`.
4. **Precondition (hard):** the running `dograh-api` image must contain the
   split-host code (commit `80368a7`). The live compose pulls
   `…/dograh-api:latest` with no build stanza — Jeeves must confirm how that
   image is produced locally and rebuild/retag it from this branch before
   restart. With an old image, removing the compose `TURN_HOST` override
   would leave the server-side peer pointed at `10.0.0.133`, which the API
   container cannot reach — worse than the status quo.

**Phase 2 — validate before restart**
1. `docker compose --profile local-turn config -q` (with the live `.env`).
2. Isolated render into a temp dir (same pattern as
   `scripts/tests/test_turn_split_host.sh`) and assert:
   `external-ip=10.0.0.133/172.28.0.8`, `static-auth-secret` matches the
   live `TURN_SECRET` (compare hashes, do not print), relay range
   49152-49200, no unexpected listeners.

**Phase 3 — apply (only the affected services)**
1. `docker compose --profile local-turn up -d dograh-init coturn api`
   (dependency order is enforced by `service_completed_successfully`).
   Do not recreate postgres, redis, minio, ui, cloudflared, the gateway,
   Jeeves Deck, or the Windows connector.
2. Verify: api health endpoint 200, coturn listening (container logs),
   restart-count delta 0, gateway connector still Ready.

**Phase 4 — sanitized audience verification**
1. Mint one short-lived authenticated media session; inspect only URI hosts:
   client credentials must carry `10.0.0.133`.
2. Confirm from API logs that server-side ICE was built with time-limited
   credentials (log prints TTL only) and, via a debug shell if needed, that
   `TURN_INTERNAL_HOST=host.docker.internal` is set in the api container.
   Never print usernames, passwords, bearer or session tokens.
3. Re-confirm GPU→`10.0.0.133:3478` TCP reachability; firewall stays
   private-LAN only (no router forwarding, no public DNS).

**Phase 5 — gated human qualification (plan Task 6, Jeeves + Maxim)**
One attended GPU call with `FORCE_TURN_RELAY=true`: Start Call → mic
permission → deterministic phrase → audible agent reply; relay candidate and
successful audio writes in Dograh logs; End Call releases the Windows mic
indicator and terminates the pipeline without timeout. Record in
`docs/qualification/2026-07-19-lan-turn-split-host.md`.

**Rollback (any failure in Phases 2-5)**
Restore the Phase-0 copies of `.env`, `docker-compose.yaml`, helper bundle,
and templates; `docker compose --profile local-turn up -d dograh-init coturn
api`; verify health and restart counts. The Windows build, pairing registry,
workflow/tool definitions, database, and Jeeves Deck are untouched by both
rollout and rollback.

## Risk assessment

- **Low, guarded by tests:** default single-host behavior — absent/empty new
  keys reproduce today's URIs, render, and setup output exactly (fallback
  tests + compat lanes).
- **Medium, rollout-time:** API image provenance (Phase 1 step 4). Hard
  precondition; wrong image makes ICE strictly worse than today. Must be
  verified before restart.
- **Medium, external-behavior:** coturn `external-ip=public/private`
  semantics. The render is proven; coturn's runtime interpretation (advertise
  `10.0.0.133`, bind `172.28.0.8`) matches documented coturn behavior and the
  live static container address, but only the live Phase-4/5 checks prove it.
- **Low:** TLS URIs (`turns:…:5349`) are still returned ahead of TCP/UDP by
  the existing ordering; unchanged behavior, and the previous live attempt
  already allocated over non-TLS TURN.
- **Not addressed (intentionally):** the legacy static-credential fallback
  still uses `TURN_HOST` server-side; irrelevant while `TURN_SECRET` is set.
- **Unproved by design:** the end-to-end GPU forced-relay voice path. Source
  work can only prove readiness; the human microphone/duplex test in Phase 5
  is the acceptance gate.

## Recommended next step for Jeeves

Review commits `80368a7`, `c82c63f`, `a7f1d5b` against the approved design
(`docs/plans/2026-07-19-lan-turn-split-host-design.md`), resolve the API
image provenance question, then execute the rollout recipe above with the
Phase-0 rollback capture and run the gated GPU qualification with Maxim.

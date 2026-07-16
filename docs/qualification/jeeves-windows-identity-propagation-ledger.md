# Jeeves Windows Identity Propagation Ledger

Date: 2026-07-15/16 PDT

This ledger records the immutable source, deployment, rollback, and protected-state boundaries for the Dograh identity-propagation run. Secret values, bearer tokens, credential payloads, transcripts, and audio are excluded.

## Source baseline

- Dograh branch: `feature/jeeves-windows-identity-propagation`
- Starting HEAD: `0a853d6443d5748c7546b368f1efa5606d1ed616`
- Base SHA: `9a2e3ea83d5019975648f46be8f346a63e817163`
- Approved design commit: `04a8f8f`
- Pinned Pipecat submodule: `228324a146a6765c6b8d610963bc80d7bc8cb9f7`
- Initial Dograh worktree status: clean.
- Jeeves Windows branch/HEAD: `feature/dograh-voice-connector` at `1f0b5cd0fcc37ec24993c1b2351c6f41f62d779d`.
- Jeeves Windows tracked status: clean. The existing untracked `.openclaw/` review directory is protected and must remain untouched.

## Deployment baseline

- Compose project: `dograh-local`.
- Compose files:
  - `/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/docker-compose.yaml`
  - `/home/mmounier/.openclaw/workspace/apps/jeeves-deck/docker/dograh-api-override.yaml`
- Running API service/container: `api` / `dograh-local-api-1`.
- Running API image/tag: `dograh-local-api:openrouter-audio`.
- Running API image digest: `sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7`.
- Deployed Dograh source identity: base `9a2e3ea83d5019975648f46be8f346a63e817163`. The deployed SHA-256 hashes of `pipecat_engine_custom_tools.py`, `custom_tool.py`, and `text_chat_runner.py` exactly match that commit.
- API health: HTTP 200 at `http://127.0.0.1:18000/api/v1/health`; container health `healthy`.
- UI availability: port 3010 redirects to `/auth/login` and returns HTTP 200; `/api/v1/health` through port 3010 returns HTTP 200. Docker reports the UI container unhealthy because its health command resolves `localhost` to unbound IPv6 `::1`; this is a pre-existing healthcheck mismatch, not an observed availability failure.

### Service inventory

| Service/container | Image | Baseline state | Published surface |
|---|---|---|---|
| API / `dograh-local-api-1` | `dograh-local-api:openrouter-audio` | healthy | `127.0.0.1:18000 -> 8000/tcp` |
| UI / `dograh-local-ui-1` | `ghcr.io/dograh-hq/dograh-ui:latest` | reachable; Docker healthcheck mismatch described above | `3010/tcp` |
| PostgreSQL / `dograh-local-postgres-1` | `pgvector/pgvector:pg17` | healthy | `5432/tcp` |
| Redis / `dograh-local-redis-1` | `redis:7` | healthy | `6379/tcp` |
| MinIO / `minio` | `minio/minio` | healthy | loopback `9000-9001/tcp` |
| TURN / `coturn` | `coturn/coturn:4.8.0` | running | existing TURN/TLS/UDP ports |
| Tunnel / `cloudflared-tunnel` | `cloudflare/cloudflared:latest` | running | existing metrics port 2000 |

### Exact rollback receipt

Rollback image digest: `sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7`.

The immutable digest remains the authority even after the mutable deployment tag is moved. Exact rollback commands:

```bash
docker image tag sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7 dograh-local-api:openrouter-audio
docker compose -f /home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/docker-compose.yaml -f /home/mmounier/.openclaw/workspace/apps/jeeves-deck/docker/dograh-api-override.yaml up -d --no-deps --force-recreate api
```

Before any restart, the rollback digest must also have a dedicated immutable local tag and the zero-active-call proof below must be repeated.

## Protected Deck pilot baseline

Protected workflow:

- ID: `1`
- UUID: `ae33eb50-0423-44a9-b431-d83432781017`
- Name: `Jeeves Deck Tool Bridge Pilot`
- Status: `active`
- Organization ID: `2`
- Released definition ID: `1`
- Published definition: ID `1`, version `1`, current/published.
- Redacted canonical workflow hash: `2aefec6a27eebd0aecb32f6a7b2b5f44db112306e8b04d61101816885781f0e2`.
- Redacted canonical published-definitions hash: `dd6fcd53f6d17bd881d6507b7b4eaf4d61bb616606f90ec64c3832d96db2216d`.
- Redacted canonical tool-inventory/definition hash: `d8c93b70deaf68a212fc50964957a680485cb712ba5269e9ef8acc08f5fe862e`.
- Redacted canonical token-metadata hash: `aa9332bc86a850977356bcba480d9d2eaebd3ed73ae140e06dc4fb3169539d89`.

Protected token metadata (token value omitted): ID `1`, workflow ID `1`, active, usage count `199`, no expiry.

Protected tool inventory:

| ID | UUID | Name |
|---:|---|---|
| 1 | `4b825854-dd2d-4537-b8e3-f055fd18ae3f` | `jeeves_query_kb` |
| 2 | `809c72d3-ee02-4dfb-89e0-3f42cc5032f0` | `jeeves_latest_call` |
| 3 | `fec1059c-9f38-4d74-b207-b85e59244621` | `jeeves_open_app` |
| 4 | `c49ac499-ee6c-4bc2-930e-eb5b61b6e8fd` | `jeeves_runtime_health` |
| 5 | `567d4eb0-c302-40a5-92c1-4fe43e0c5e73` | `jeeves_window_action` |
| 6 | `f4e60a49-3f6f-4253-931a-8448b5b99e5d` | `jeeves_session_action` |
| 7 | `a9aaea6a-410c-478f-b765-63beb61a26ac` | `jeeves_geo_dashboard_reposition` |
| 8 | `19a78db6-4388-43b6-8e4c-f7ef4c456a66` | `jeeves_web_search` |
| 9 | `f87e796a-fdcc-4c6f-a4fc-2ee22ef45ef6` | `jeeves_plan_action` |
| 10 | `16fac780-6587-49c7-a4e0-c2600b335451` | `jeeves_plan_draft` |
| 11 | `511ee842-ca43-4e9a-8193-e30e10ca870e` | `jeeves_plan_query` |
| 12 | `4cc29166-4963-4e7f-b7b3-8e1a0262bd8e` | `jeeves_world_cup_navigate` |
| 13 | `97a2ef3b-4deb-446e-ab4a-a943d5345736` | `jeeves_world_cup_query` |

All protected tools are active HTTP tools owned by organization `2`. The raw workflow/tool/token exports were canonicalized in-process for hashing and were not written to disk. Token values and credential material were excluded from the token export.

## Zero-active-call proof

At the Task 1 observation boundary:

- Two immediate `/proc/net/tcp*` inspections in the one-worker API container found zero established inbound sockets on API port 8000.
- PostgreSQL contained zero unexpired embed sessions. The newest embed session expired on 2026-07-12.
- PostgreSQL contained zero text-session updates in the prior hour. The newest text-session update was 2026-07-11.
- API logs since the current container start contained no WebRTC start/connect, text-chat request/start, disconnect, error, exception, or traceback matches.
- Historical `workflow_runs` rows still include stale `initialized`/`running` flags, but their newest creation time is 2026-07-12 and they have no corresponding live session, recent text checkpoint, socket, or current-process activity. They are not treated as live calls.

Conclusion: zero active calls is proven at this observation boundary through runtime sockets plus the authoritative session/run database. This proof expires immediately when external state changes and must be repeated immediately before deployment.

## Mutation boundary

No live object, image tag, service, endpoint, port, credential, workflow, tool, token, or provider was changed while creating this baseline. The Deck pilot and all 13 tools are inspect/compare-only protected state.

## Dograh implementation and regression gates

Identity transport commits:

- `4731068` — `feat: support reserved HTTP tool runtime identity`
- `4462a63` — `feat: propagate Pipecat tool call identity to HTTP tools`

TDD and focused evidence:

- Executor transport: eight new identity cases failed before implementation, then passed after implementation.
- Handler propagation: five new propagation cases failed before implementation, then passed after implementation.
- Final focused regression: `api/tests/test_custom_tools.py` — 59 passed.
- Changed-file Ruff import/F401 check: passed.
- Changed-file Ruff format check: passed.
- Targeted mypy for both changed production modules: passed with zero issues.
- `git diff --check`: passed.

Full API regression evidence used an isolated `dograh_test` PostgreSQL database, Redis database 15, and local TypeScript-validator dependencies. It did not use or mutate deployed application data. The result was 913 passed and two failures. Both failures are the pre-existing suite-order class-identity assertions in `api/tests/test_openrouter_audio_provider.py`; that unchanged file passes 7/7 when run alone, and neither it nor its implementation is part of this change.

The repository-wide wrapper gates have unrelated baseline defects: `scripts/lint.sh` passes an unsupported `--check` option to the installed Ruff, direct full-tree Ruff reports 125 pre-existing findings, and full-tree mypy has a pre-existing error backlog. Changed production and test files are clean under the focused formatter/lint/type gates above.

## Immutable candidate and rollback image receipt

- Candidate source SHA: `c202e8b5bba1be2164746b9060e5b12da43599e6`.
- Candidate tag: `dograh-local-api:c202e8b5bba1`.
- Candidate immutable digest: `sha256:7bbc1bdd6a307ed14be991568f3806f9ce89bea8935f18c3cf4e6bba93a5ca02`.
- Rollback tag: `dograh-local-api:rollback-4a9b5ca87df6`.
- Rollback immutable digest: `sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7`.
- The mutable deployment tag and running API remained on the rollback digest throughout this build/smoke task.

The image was built directly from the clean committed worktree through `api/Dockerfile`, without moving the deployment tag. The candidate's two changed production-file hashes exactly match the files at the source SHA:

- `custom_tool.py`: `10ee9b9e9626ecc36f9bb75830a300b1ac57f3db4f1c419cc6628e6d9ea7f988`.
- `pipecat_engine_custom_tools.py`: `29492cd730f843c619f911bcea0075c028862c40e8e027ef225afe04c8e486c9`.

An unexposed ephemeral candidate container used only the isolated `dograh_test` database and Redis database 15. Its internal `/api/v1/health` returned HTTP 200. A no-network executor smoke proved the opted-in header and unchanged model body, fail-closed missing identity before I/O, and byte-equivalent legacy request shape. The startup script validation also passed. The ephemeral container was removed, and no protected Deck workflow/tool/token or deployed application data was read or changed by these image checks.

## Controlled API-only deployment receipt

Immediately before the restart, two API socket observations found zero established connections on port 8000. PostgreSQL reported zero unexpired embed sessions, zero text sessions updated in the preceding hour, and zero workflow runs created in the preceding hour. The running API was still healthy on the rollback digest, both candidate and rollback tags were addressable, and the four Task 1 protected hashes matched exactly.

Only `dograh-local-api-1` was recreated through the existing two-file Compose procedure. No other service, port, endpoint, certificate, provider setting, credential, workflow, tool, or token was changed.

- Deployment time: 2026-07-16 00:40 PDT.
- Running/mutable-tag digest: `sha256:7bbc1bdd6a307ed14be991568f3806f9ce89bea8935f18c3cf4e6bba93a5ca02`.
- Rollback tag/digest remains: `dograh-local-api:rollback-4a9b5ca87df6` / `sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7`.
- Container state: running and healthy.
- Direct API health on port 18000: HTTP 200.
- Port-3010 API health and redirected UI root: HTTP 200.
- In-container PostgreSQL and Redis pings: passed.
- Deployed changed-file hashes: exact candidate matches.

Post-restart Deck comparison matched Task 1 exactly:

- workflow: `2aefec6a27eebd0aecb32f6a7b2b5f44db112306e8b04d61101816885781f0e2`;
- definitions: `dd6fcd53f6d17bd881d6507b7b4eaf4d61bb616606f90ec64c3832d96db2216d`;
- tools: `d8c93b70deaf68a212fc50964957a680485cb712ba5269e9ef8acc08f5fe862e`; and
- redacted token metadata: `aa9332bc86a850977356bcba480d9d2eaebd3ed73ae140e06dc4fb3169539d89`.

The protected workflow remained ID 1 / UUID `ae33eb50-0423-44a9-b431-d83432781017`, active on released definition 1; all 13 protected tools remained active; protected token ID 1 remained active at usage count 199. The rollback trap was not invoked.

## Deployed fake-Windows identity qualification

An internal-only instance of Jeeves Windows gateway commit `e878830c4ee1456dc59aa5abe854fd1598847326` ran on the existing Docker network with the repository's `FakeWindows`. It published no host port and used no provider, Deck backend, or real Windows path.

Disposable staging receipt: `/home/mmounier/.openclaw/workspace/state/runs/dograh-jeeves-windows-identity-propagation-staging-receipts.json` (mode 0600). Exact created-and-deleted records:

- workflow ID 2 / UUID `7685ea89-67ec-415c-8bad-c9ec4eb6a3df` / definition ID 2;
- `windows_check_last_action`: tool ID 14 / UUID `1963b5e0-ebfc-4f9a-a35c-9c9025c6f8c3`;
- `windows_find_app`: tool ID 15 / UUID `02d2a202-2561-4a4d-8b16-a01f5ffed160`;
- `windows_focus_window`: tool ID 16 / UUID `fdd1cda3-b946-4551-bf52-c72be3ab2fd8`; and
- `windows_open_app`: tool ID 17 / UUID `419566d0-f37e-4060-ab61-bbf186455c8a`.

The deployed handler/executor produced these results:

- synthetic text and voice lanes each preserved stable identity into a durable gateway mapping and returned HTTP 200 fake-Windows truth;
- repeating the same logical call produced `cached=false` then `cached=true` with exactly one Windows invocation;
- model-authored reserved identity fields failed with HTTP 400;
- malformed identity failed with HTTP 400;
- wrong `agent_scope` failed with HTTP 403;
- an unknown tool route failed with HTTP 404;
- a 100 ms Dograh timeout returned a structured timeout error and propagated gateway cancellation; and
- cancelling the in-flight Dograh handler propagated `CancelledError`, issued no callback result, and propagated gateway cancellation.

The fixture delta was five unique Windows invocations and two cancellations. No retry produced a second fake effect. Dograh logged only the SHA-256 identity correlation prefix and argument-key list at the handler layer; no raw runtime envelope or credential was emitted.

The staging receipt printer initially encountered SQLAlchemy post-commit expiration after its transaction committed. Creation was not retried: exact IDs were recovered read-only through the unique marker and written immediately to the mode-0600 receipt. Cleanup then verified every ID/UUID/name relationship, zero runs, zero tokens, and the exact definition relationship before deleting only those disposable rows in one transaction. The fixture container and temporary runner were removed. All four protected Task 1 hashes returned exactly to baseline after cleanup.

## Dedicated Windows voice agent

The persistent `Jeeves Windows Voice` workflow is independently owned by organization/user `3`, active, and published on definition ID `3` version `1`. Its exact workflow identity is ID `3` / UUID `15af4c02-3b83-4483-a529-fa73c735dbc9`; embed token ID `2` remains active with no expiry or usage limit. The four attached active tools are Windows-owned IDs `18`-`21` with UUIDs `e8f3025e-4495-44a6-b0d2-0cf9d3cb4063`, `7116800c-d985-4289-943b-42e2196968b5`, `52a8f327-895d-4137-a0bb-d96579bb8fcc`, and `aa669365-7fa3-441c-8f0f-e7c31a903b9b`.

Every stored tool definition opts into the reserved identity transport, carries `agent_scope=jeeves_windows`, and targets only `http://jeeves-windows-voice-gateway:8080/tools/windows_*`. Credential ID `1` / UUID `58773313-db7d-46c4-ad75-42ec6ad30ee5` is active and Windows-owned. The combined redacted tool-manifest hash is `bf8c4bdeca45e3c8a97259e867c7d2f7658a0a3afdec2c6881b0b8e8d9a90c28`. The exact persistent receipt is `/home/mmounier/.openclaw/workspace/state/runs/dograh-jeeves-windows-identity-propagation-final-objects.json` at mode `0600`; it contains no secret values.

The stored definitions returned HTTP 200 through deployed Dograh for four fake-backed positive routes. Five ambiguous utterances produced clarification-only behavior and zero gateway dispatches. Public embed configuration and initialization returned HTTP 200, creating disposable workflow run ID `236` and embed session ID `200`; both were deleted by exact ownership-checked ID, and a fresh configuration request remained HTTP 200. No Deck object, route, backend, credential, source tree, or capability participates in this workflow.

## Final Task 12 verification

The final Dograh focused gate passed `59/59`. Changed-file Ruff check/format, targeted mypy with skipped import following, and `git diff --check` passed. The final isolated full-suite environment used `dograh_test`, authenticated Redis database 15, and no deployed application data. It collected 911 tests and completed with 909 passed plus the same two pre-existing suite-order class-identity failures in `test_openrouter_audio_provider.py`; that unchanged file immediately passed `7/7` alone. An earlier regression run recorded 913 passed plus the same two failures. No in-scope identity test failed.

The Jeeves Windows gateway final gates passed Biome, TypeScript, and the complete Vitest suite: 7 files / 63 tests, including the 1,000-iteration duplicate/restart/cancel matrix. The separately invoked dedicated stress command also passed `1/1` in 260.2 seconds.

Final read-only live verification found:

- API health HTTP 200 and a healthy container on candidate digest `sha256:7bbc1bdd6a307ed14be991568f3806f9ce89bea8935f18c3cf4e6bba93a5ca02`;
- the expected port-3010 redirect, plus successful PostgreSQL and authenticated Redis pings;
- two consecutive API socket observations at zero established inbound port-8000 connections;
- zero unexpired embed sessions, zero text sessions updated in the preceding hour, and zero workflow runs created in the preceding hour;
- the persistent workflow still active on released published definition `3`, token `2` still active, credential `1` still active, and all four Windows tools still active on their recorded internal-only routes; and
- no persistent gateway fixture container and no real Windows/GPU action.

The final protected Deck hashes exactly match Task 1:

- workflow: `2aefec6a27eebd0aecb32f6a7b2b5f44db112306e8b04d61101816885781f0e2`;
- published definitions: `dd6fcd53f6d17bd881d6507b7b4eaf4d61bb616606f90ec64c3832d96db2216d`;
- exact tool IDs `1`-`13`: `d8c93b70deaf68a212fc50964957a680485cb712ba5269e9ef8acc08f5fe862e`; and
- redacted token metadata: `aa9332bc86a850977356bcba480d9d2eaebd3ed73ae140e06dc4fb3169539d89`.

The rollback image remains locally addressable as `dograh-local-api:rollback-4a9b5ca87df6` at `sha256:4a9b5ca87df6c8724c4a2b92a69ea44f301791583b3d106cbf5e17dfab2c29e7`. The rollback trap was not invoked.

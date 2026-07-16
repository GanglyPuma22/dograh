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

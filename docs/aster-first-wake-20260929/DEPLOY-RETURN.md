# Aster first wake API deployed

Completed **2026-09-30T06:26:14Z** in Maxim's explicit API service-change window.

- Deployed image: `dograh-api:aster-first-wake-candidate-20260929`.
- Image ID: `sha256:258e614f718fe94caf811628a89fd8e37f5bea286b0a2233c15ee98151463bb1`.
- API container: `831fb6ef913b`, started `2026-09-30T06:25:36.114953301Z`, running and Docker-healthcheck **healthy**.
- HTTP `/api/v1/health`: **200**. Authenticated `hello_ack`, protocol 1: **`game_events_v1` and `aster_wake_v1`**. Only hello was sent; no turn, microphone audio, event or provider conversation was invoked.

Preflight verified the original bundle manifest SHA-256 `5a1a7a38cf6328d21e5d5db375bd60053d646e67b816ef36c6cef0467c92bb4a`, its patches/evidence, backend/game candidate inventories, every matching actual backend working-input hash, and all four Dockerfile source/destination paths. No drift was found. The four deployed runtime file hashes also match the tested candidate exactly. The earlier uncommitted station-event work remains intact; the backend working checkout was not patched or reset.

Only the API was replaced using compose project `dograh-salvage-live`, its existing three compose files and the wake-image override, with `--no-deps --no-build --pull never`. Private API environment values were retained in memory for compose interpolation and checked equal after deployment; none were printed or exported. PostgreSQL, Redis, MinIO and both existing voice-sidecar container IDs/start times remained unchanged. No provider/configuration changes, game edits, Godot launch, performance run, commits or pushes occurred.

Rollback image retained: `dograh-api:aster-events-20260925`, ID `sha256:b1d20b50f092ac3ab8fd29af4a2c5c2a2fffbb3ac3a2d2bfc80370892fd2e5bf`. Health/capability checks passed, so rollback was not needed. An initial streamed-context build failed before changing any service; the same verified Dockerfile succeeded with a minimal directory context containing its four unchanged source files.

Evidence: `/home/mmounier/src/aster-first-wake-20260929/artifacts/deployment-20260930T062520Z/` contains `preflight.json`, `build.json`, `build.log`, `result.json` and `runtime-hashes.json`. Exported evidence is under the Windows handoff's `wsl-candidate/artifacts/deployment-20260930T062520Z/`. The earlier failed-build evidence is preserved alongside it.

No deployment problem remains. Human audio acceptance is pending through Windows `PLAY-ASTER-WAKE.cmd`. Windows integration reports **1,250 assertions passed in 14 files** and its bridge/test-double fixes; that was not rerun here. The reported Kit child-process/certificate-store limitations leave formal native qualification pending. Neither backend tests nor this authenticated handshake establish heard audio.

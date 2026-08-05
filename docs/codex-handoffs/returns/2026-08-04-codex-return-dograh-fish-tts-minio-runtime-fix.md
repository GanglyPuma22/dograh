# Codex Return: Dograh Fish TTS and Salvage MinIO Runtime Fix

## Outcome

The direct-Fish regression is fixed and proven against the live provider without
launching Salvage. Fish currently returns raw PCM as `audio/pcm`; Dograh `main`
rejected that response because it required exactly `application/octet-stream`.
The adapter now accepts both verified raw-PCM media types while continuing to
reject JSON, text, MP3, and other incompatible responses.

The Salvage Compose profile now supports configurable loopback MinIO API and
console host ports. Defaults remain 9000/9001. The GPU laptop uses 19000/19001,
with `MINIO_PUBLIC_ENDPOINT` aligned to the API host port.

Maxim subsequently completed the in-game acceptance gate. Aster's Fish replies
were audible, and interrupting an active reply worked as intended.

## Source and review

- Repository: `/home/mmounier/src/dograh-fish-tts-minio-runtime-fix`
- Branch: `fix/fish-tts-minio-runtime-config`
- Base: `c423edc6e60bb7081d41e14cf81a0b77e1164020`
- Fish commit: `a525274` — `fix: accept verified Fish PCM response contract`
- MinIO/return commit: second branch commit — `fix: make Salvage MinIO host ports configurable`
- PR: https://github.com/GanglyPuma22/dograh/pull/9
- PR state at this update: open, awaiting refreshed checks after review fixes
- Codex review: no major issues found
- Claude review: approved with non-blocking suggestions
- Addressed review feedback: Fish advertises both accepted PCM media types;
  missing response content type and PCM metadata are covered by tests; the
  public MinIO endpoint derives from its API host port; the Compose test ignores
  repository `.env`, handles signals, and asserts the exact port set; the test
  and anonymous local-bucket assumption are documented.
- Deferred as out of scope: parsing an optional Fish `rate=` media-type
  parameter and adding a new CI workflow. Neither is required by the verified
  provider response or accepted runtime behavior.

## Changed files

Fish response contract:

- `api/services/game_companion/providers.py`
- `api/tests/test_game_companion_providers.py`

MinIO configuration and documentation:

- `docker-compose.salvage.yaml`
- `api/.env.salvage.example`
- `docs/developer/game-companion-protocol-v1.mdx`
- `scripts/tests/test_salvage_compose_config.sh`

Return receipt:

- `docs/codex-handoffs/returns/2026-08-04-codex-return-dograh-fish-tts-minio-runtime-fix.md`

## Fish evidence

Exactly two fixed, benign provider requests were made. No response audio was
saved or played.

Before the fix, the current request shape returned:

- HTTP 200
- normalized content type `audio/pcm`
- no content length
- chunked transfer encoding
- 82,336 total bytes
- nonzero, even byte count
- no JSON/text, RIFF/WAV, ID3, or MP3 signature
- apparent raw binary
- first response byte in 661.4 ms

Current `main` rejected that response before reading its body. A focused
regression test reproduced the rejection.

After the fix, the repaired adapter returned:

- HTTP 200
- normalized content type `audio/pcm`
- no content length
- chunked transfer encoding
- first valid PCM chunk in 319.0 ms
- 52 PCM chunks and 93,482 total PCM bytes
- nonzero, even-length 24 kHz mono PCM
- clean stream completion

The request used fixed synthetic text. The configured voice reference was used
but its identifier was neither displayed nor retained. Same-turn fallback
behavior was not changed.

## Automated validation

- `api/tests/test_game_companion_providers.py`: 49 passed.
- All `test_game_companion_*.py` files: 287 passed.
- Full `api/tests/` suite against isolated temporary Postgres/Redis: 1,290
  passed, 9 warnings, 0 failures.
- `scripts/tests/test_salvage_compose_config.sh`: default ports, derived
  alternate endpoint, and explicit endpoint override passed in an environment
  isolated from repository `.env`.
- Ruff check and format check on changed Python files: passed.
- Targeted MyPy on `providers.py`: passed.
- A broader MyPy import traversal was not clean because of existing unrelated
  errors in filesystem and JSON-parser modules; none were in changed files.
- `bash -n scripts/tests/test_salvage_compose_config.sh`: passed.
- `git diff --check`: passed before commits.

The temporary full-suite database, Redis container, and Docker network were
removed after the run. They did not use or alter live Dograh data.

## Runtime receipt

- Docker project: `dograh-salvage-live`
- Services recreated: `postgres`, `redis`, `minio`, `api`
- API service/image tag: `api` / `dograh-api:salvage-local`
- Provider-proof API image: `sha256:9f773be735a9c5b0d846108086c854769acdbb1920025b72c3938a353f187898`
- Provider-proof API container: `101c907e03bd`
- Compose working directory: the branch worktree above
- Host/container provider-source digests matched
- API: `127.0.0.1:8000`
- MinIO API: `127.0.0.1:19000 -> 9000`
- MinIO console: `127.0.0.1:19001 -> 9001`
- Internal MinIO endpoint remained `minio:9000`
- API and both MinIO endpoints returned HTTP 200 from WSL and Windows
- Missing companion token: 403
- Incorrect companion token: 403
- Correct companion token: 101, V1 `hello_ack`, companion Aster
- Windows user and backend companion tokens: present and matching

Preserved volumes:

- `dograh-salvage-live_postgres_data`
- `dograh-salvage-live_redis_data`
- `dograh-salvage-live_minio-data`
- `dograh-salvage-live_shared-tmp`

The unrelated transcription container retained the same container identity,
August 1 start time, restart count 0, and host port 9000 binding. It was not
stopped, restarted, recreated, or reconfigured.

## Logs and safety

The sanitized API log is available locally at
`/tmp/dograh-api-fix-sanitized.log`. Startup completed normally, authentication
showed the expected two 403 rejections and one accepted WebSocket, and there was
no Fish unsupported-content-type, provider-configuration, traceback, or API
startup error. The only recurring warnings were the already-known harmless
cloudflared metrics timeouts/name-resolution warning.

Configured companion, OpenRouter, Fish, JWT, and reference-voice values were
checked by exact comparison and were absent from raw API logs. No secret is in
this document, the commits, or the PR text.

Codex did not launch, build, or modify Salvage. It did not operate Aptivio
services, alter Salvage PRs, delete Docker volumes, merge the Dograh PR, or make
any source change outside Dograh.

## Acceptance and remaining risk

The provider boundary and the human end-to-end gate are both accepted. Maxim
heard Aster's Fish replies in the existing Salvage build and successfully
interrupted an active reply. No further game launch is required for this fix.

The remaining low-risk hardening items are optional Fish `rate=` parameter
validation and wiring the focused Compose test into a new CI job. Neither
changes the accepted PCM contract or the reviewed runtime behavior. Merge PR #9
after its refreshed required checks complete successfully.

# Aster first wake: source candidate ready for integration

Backend implementation, fake-provider regressions and game-patch source review are complete. Nothing was installed into the playtest or deployed to Dograh. The running game has not been verified to speak its wake greeting.

This return is at `/home/mmounier/src/aster-first-wake-20260929/WSL-RETURN.md`. The requested Windows output directory is outside this worker's writable roots, so the return is provided here as permitted by the handoff.

## Candidate and identity

- Owned bundle: `/home/mmounier/src/aster-first-wake-20260929`.
- Backend candidate: `backend/api`, copied from the actual working API under `/home/mmounier/src/dograh-fish-tts-minio-runtime-fix`; no credentials or `.env` files copied. Preserves the pre-existing uncommitted station-event implementation. Backend baseline HEAD: `395050d6473e21bd58d1d32fd44e539521a23307`; HEAD alone is insufficient.
- Game candidate: `game-candidate/game`, copied from `/mnt/c/Users/maxim/Documents/SalvageAsterNight-20260929/game`, retaining the actual working prototype. Generated caches, artifacts and authoring `source/` were excluded. It is an unimported source candidate; it has no Windows host configuration or copied top-level launcher tools.
- `backend.patch` is a six-file delta against the actual working backend; `game.patch` is the reviewed five-file delta against the actual working game. Each patch passed `git apply --check` against an owned baseline copy and, when applied there, reproduced every candidate file byte for byte, including mixed source line endings. Original five game input hashes still exactly match the supplied manifest.
- Full input/output hashes: `manifest.json`. Backend full inventory: `artifacts/backend-inventory.json`. Game inventory and independent pending native verdicts: `game-candidate/game/artifacts/candidate.json`.

| Artifact | SHA-256 |
|---|---|
| Backend inventory JSON | `7d516457306f1dacb3609d567efc1dddbeaca81c11ca119e73b8e4fdf6225a6b` |
| Game content digest (Kit inventory) | `960bed7a0bcfda58d39f1d086d20214dbc41073955b49afa0cd6dadccd884356` |
| backend.patch | `d7d3ff0cd7e673dad895cfd56488a61567079998f3e1551e958c9119dd0138b1` |
| game.patch | `a118f2eedb656e4478d2a5a9cd4dfd7f7ea5bf228a0b8520359fc3e3299f1d29` |

## Changed files and behavior

Backend:

- `api/services/game_companion/protocol.py`: allowlisted `aster_first_wake`; requires actual boolean `activated: true`, `phase: "awake"`, actual boolean `first_activation_this_session: true`; no assay requirement. Bounded invalid/unknown game events with valid IDs yield recoverable errors before order/session mutation, keeping current speech and the connection alive. Invalid IDs and oversized envelopes retain protocol failure handling.
- `api/routes/game_companion.py`: advertises `aster_wake_v1` alongside `game_events_v1`; routes recoverable event-validation errors to their request IDs.
- `api/services/game_companion/session.py`: creates event turns with input closed from the initial emitted state; no listening presentation or transient open input. Reuses existing no-STT, no-tools LLM/TTS path and generation/ownership suppression.
- `api/services/game_companion/persona.py`: short first-meeting introduction using the existing text and generic talk-key wording. Both `recent_aster_opening` and `recent_station_opening` remain available as reported dialogue but are separated from authoritative world JSON and explicitly do not prove heard audio.
- `api/tests/test_game_companion_route.py`: authenticated fake-provider wake transport and rejection of invalid/unknown events during active speech.
- `api/tests/test_game_companion_wake.py`: strict validation, closed input, ordinary reply compatibility, no STT/player-caption/actions, deliberately cancellation-resistant late LLM/TTS completions, tool-call rejection and caption grounding.

Game:

- `game/scripts/companion/companion_client.gd`: event-specific capability/validation guards; closed-input game event; actual final wake captions ground one ordinary reply, with interrupted/muted delivery caveat. Bench remains the default event and retains its earned-assay gate/context key. Wake booleans are checked by type as well as value.
- `game/scripts/companion/lilt/lilt_encounter.gd`: fresh activation only, after wake animation; existing opt-in, 30 seconds awake to find a ready idle service, one accepted request, no restored-save replay. Cancellation targets only the owned greeting's turn/audio and preserves a newer player reply. Review corrections cancel departure/modal/boarding/recovery during the wake animation too, and prevent the greeting's own playback from dismissing or claiming delivery of the independent text introduction. Invalid scene transforms cancel a pending greeting.
- `game/tests/test_companion_client.gd`: capability negotiation, malformed/unearned wake, no microphone/end-turn, reply caption continuity, expiry/reconnect/bench compatibility, strict boolean cases.
- `game/tests/test_lilt_encounter_integration.gd`: one-shot activation, animation ordering, saved-awake reload, unavailable-service expiry, cancellation, player priority and own-playback text fallback.
- `game/tests/aster_event_check.gd`: includes the Lilt integration tests in the existing focused runner.

## Commands and results

`python3 /home/mmounier/src/aster-first-wake-20260929/run_backend_tests.py`

Result: **342 passed, 1 warning, 12.79 seconds**. The warning is the existing Starlette/AnyIO `BlockingPortal` deprecation. No unresolved backend test failures. Exact container command and exit code are in `artifacts/backend-test-result.json`; output/provenance in `artifacts/backend-tests.log`. Pytest 9.1.1 / pytest-asyncio 1.4.0, automatic asyncio mode. All four runtime modules were imported from `/candidate/api`, verified in the log. The test containers had networking disabled, read-only source, fake database/Redis values, one CPU, 512 MB memory and no GPU/provider credentials. No test database or real provider was contacted.

Initial diagnostic runs needed correction of missing dummy environment variables, asyncio mode and container working directory; a new route test also initially expected an idle acknowledgement that the existing interrupt protocol does not send. Those issues were corrected before the final retained passing run. The runner refuses to overwrite its retained log; use a separately named evidence output for a future run.

`python3 /home/mmounier/src/aster-first-wake-20260929/parse_game.py`

Result: **5/5 files parsed**, gdtoolkit 4.5.0, receipt `artifacts/gdscript-parse.json`. Parser/dependencies/cache are local to the bundle; no global settings changed. This is syntax parsing only, not Godot type analysis, import, test execution, scene lifecycle or signal-order verification.

`python3 /home/mmounier/src/aster-first-wake-20260929/package_candidate.py`

Result: both patches passed application checks and reproduced the candidate bytes in owned scratch copies. The packaging script creates new owned scratch directories on rerun and checks the five game baseline hashes before packaging.

Game Studio Kit 0.1.6 `doctor`, `candidate --id aster-first-wake-source-20260929 --engine-version 4.7.stable`, and `validate-record --record artifacts/candidate.json` were run against the owned game root. Candidate validation returned `{"ok": true, "kind": "candidate"}`. That establishes inventory consistency only. The copied project record retains historical playtest launch metadata; use the fresh identity below, not its `aster-night-20260929` route or expired production time window. All visual, interaction, motion, audio and performance verdicts remain `not_run`; human acceptance is pending. Routing scope/decisions are in `artifacts/WORK-CARD.md`.

## Integration and service-change prerequisites

1. Wait until Maxim finishes the current playtest. Preserve all current saves, uncommitted integration work and isolated review identities. Respect Performance Claude's next exclusive 45-minute machine window; native game/voice qualification must be scheduled afterward. **No service restart until the integration session explicitly opens a service-change window.**
2. Recheck `manifest.json` input hashes against both actual working targets. If they drifted, reconcile the delta; do not replace them with clean HEAD. Integrate the game patch into a separately owned Windows candidate containing the current working game plus its launcher tools/approved host config. Apply the backend patch only to its matching working baseline (or retain this owned backend candidate for the image overlay). The patches preserve the previous station-event changes.
3. Import the game with the approved Game Studio Kit Windows host configuration and pinned Godot 4.7 engine. The existing launcher's pinned engine SHA-256 is `26e48e5dea1a28d10561aa2e914f81e5166054b6f1ad6619bddbe2e49f3d28b6`; verify it against the integration host rather than substituting an engine. After import, run the focused `res://tests/aster_event_check.gd` via Kit `launch --mode test`, with the owned Windows candidate, explicit host config, that engine SHA, timeout/cutoff and a unique label. Require clean classified engine logs plus the runner's assertion verdict; parsing alone is insufficient.
4. In the explicit service-change window, verify the local runtime base tag still identifies `sha256:b1d20b50f092ac3ab8fd29af4a2c5c2a2fffbb3ac3a2d2bfc80370892fd2e5bf`, then build the provided four-file overlay recipe. No new provider, prerecorded audio or configuration changes are needed. Keep the existing token/Fish/OpenRouter configuration private and unchanged.

Future build command (not executed):

```bash
docker build --pull=false -f /home/mmounier/src/aster-first-wake-20260929/Dockerfile.wake-candidate -t dograh-api:aster-first-wake-candidate-20260929 /home/mmounier/src/aster-first-wake-20260929
```

The read-only live inspection found compose project `dograh-salvage-live`, working directory `/home/mmounier/src/dograh-fish-tts-minio-runtime-fix`, and API container `c10b683afca3`, still using the 20260925 image, started `2026-09-25T07:12:25.898125157Z`, healthy. Use its same three compose files and append the provided candidate image override. Preserve the existing compose environment-loading method; never print resolved secret configuration.

Future API-only replacement command, **only after the explicit service-change window** (not executed):

```bash
docker compose -p dograh-salvage-live -f /home/mmounier/src/dograh-fish-tts-minio-runtime-fix/docker-compose.yaml -f /home/mmounier/src/dograh-fish-tts-minio-runtime-fix/docker-compose.salvage.yaml -f /home/mmounier/src/dograh-fish-tts-minio-runtime-fix/docker-compose.aster-events.yaml -f /home/mmounier/src/aster-first-wake-20260929/docker-compose.wake-candidate.yaml up -d --no-deps --no-build --pull never api
```

5. Verify API health and authenticated `hello_ack` includes both capabilities. From the separately imported Windows candidate, use its existing launcher with `--aster-station-events --review-id aster-first-wake-20260930 --daylight --tutorial off` (choose another unused identity if that one already exists). This isolates fieldwork/tutorial/companion saves; never reset `aster-night-20260929`. The first greeting requires a fresh save; an already-awake save must remain awake without replay.
6. Human check: wake after the animation, hear one short greeting without holding the talk key, then hold the currently displayed binding and reply. Recheck interruption mid-greeting, departure/modal, unavailable service, awake-save reload, earned bench opening and its first reply. Captions and local playback are independent acceptance evidence. Record Maxim's playtest verdict.

## Service and acceptance status

No original backend/playtest source was edited. No saves were edited/reset. No Dograh service/container was stopped, restarted, recreated or reconfigured. Only disposable, network-disabled regression containers were run. No commits, pushes, merges, desktop/GPU/Blender/Godot launch or real voice-provider call occurred.

Remaining checks: Godot import/type/signal/runtime regression execution and later ordinary-input/audio acceptance. No source-review defect is known to remain, but engine and human qualification are not claimed.

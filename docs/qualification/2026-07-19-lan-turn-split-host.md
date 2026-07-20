# LAN TURN split-host review and rollout qualification

Date: 2026-07-19 PDT / 2026-07-20 UTC

Status: source reviewed and live pre-human gates passed; one attended GPU-laptop forced-relay call remains.

## Source review

Claude's implementation range `ac324a5..1dd0106` correctly separates the client-visible TURN host from Dograh's server-side route and renders an explicit coturn public/private NAT mapping. Independent review found one completeness gap: the legacy static-credential server path still used the public host. Commit `87fd49e` adds a red-to-green regression test, moves that path to `TURN_INTERNAL_HOST`, and documents `TURN_EXTERNAL_IP` in the environment-variable reference.

Fresh review evidence:

- split-host and adjacent security suites: 43 passed;
- deployment/setup script assertions: 22 passed, including Windows PowerShell 5.1;
- full API suite: 953 passed, 4 failed;
- the four failures match the pre-change baseline and do not intersect TURN routing (`NoopFeedbackObserver.emit_media_start` in two pipeline tests and module-identity assertions in two OpenRouter provider tests);
- live compose interpolation and isolated coturn rendering passed;
- `git diff --check` passed.

## Rollout receipt

Rollback state is preserved at `/home/mmounier/dograh-rollback-20260719-2211`. The previous API image remains tagged `dograh-local-api:rollback-fc5003b9` with image ID `sha256:fc5003b9d5ed095f4f004748147b5a699380a1c9a67c8aa30ee91f5eae4d1988`.

The live API was rebuilt from reviewed commit `87fd49e` and deployed as image ID `sha256:4f8ef304022de441c6e37a58c2b539f1032ab9b0c030f7245c882fd8f82a7bd3`. Only `dograh-init`, coturn, and the API were applied/restarted. The gateway and its Windows connector session were not restarted.

Sanitized live configuration:

- client-visible TURN host: `10.0.0.133`;
- API-internal TURN host: `host.docker.internal`;
- coturn mapping: `10.0.0.133/172.28.0.8`;
- relay range: UDP `49152-49200`;
- forced relay: enabled.

Post-rollout evidence:

- API container healthy with zero restarts;
- coturn running with zero restart failures and the reviewed mapping loaded;
- gateway healthy with zero restarts;
- API container TCP connection to `host.docker.internal:3478` succeeded;
- authenticated public embed initialization and header-authenticated TURN retrieval succeeded;
- public TURN response contained four URIs, all on `10.0.0.133`, with the expected short-lived credential TTL;
- Dograh's server-side ICE configuration contained one TURN entry on `host.docker.internal` with time-limited credentials;
- no credential, token, username, or password value is stored in this receipt.

## Remaining human gate

On the main GPU laptop, perform exactly one call while `FORCE_TURN_RELAY=true`:

1. Confirm `Connector: Ready`.
2. Press **Start Call** once and allow microphone access if prompted.
3. Wait up to 20 seconds, then say: “Hello Jeeves, can you hear me?”
4. Record the displayed Call, Media, and Microphone statuses and whether spoken audio is heard.
5. If connected, press **End Call** and confirm the Windows microphone indicator turns off.
6. If it fails, do not retry; correlate the single attempt against gateway, API, and coturn logs.

Success requires a relay candidate, successful audio writes, audible duplex response, clean pipeline termination, and microphone release. Any failure triggers the documented rollback rather than repeated attempts.

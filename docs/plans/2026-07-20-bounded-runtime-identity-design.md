# Bounded HTTP Tool Runtime Identity Design

## Goal

Make Dograh HTTP tool calls independent of provider-specific tool-call ID length so the Jeeves Windows voice agent can reliably execute `find_app`, `open_app`, and `check_last_action`. The human acceptance gate is that Spotify visibly launches on the main GPU laptop; playback and login are out of scope.

## Observed failure

Dograh workflow run 246 proved the voice, TURN, connector, and result-delivery path. `find_app("Spotify")` succeeded when the provider tool-call ID was 460 characters. `open_app` and `check_last_action` were rejected by the gateway as `invalid_runtime_identity` when their IDs were 700 and 1,268 characters. The gateway intentionally caps the identity member at 512 characters.

The provider ID is an opaque transport artifact. Forwarding it verbatim makes authorization and idempotency depend on a model/provider serialization detail.

## Options considered

1. **Canonical bounded identity in Dograh (selected).** Hash every nonempty provider tool-call ID into a versioned, fixed-size identity before it crosses the HTTP boundary. This preserves retry identity and call separation without weakening gateway validation.
2. **Raise the gateway limit.** This is smaller but continues forwarding arbitrary provider metadata and merely moves the failure threshold.
3. **Version the entire runtime-identity envelope.** This is extensible but requires coordinated changes in both services without adding value to this milestone.

## Architecture

Dograh remains the owner of model/provider runtime context. For opted-in HTTP tools it validates the provider tool-call ID, derives `tcid:v1:<sha256-hex>`, and places only that canonical value in `X-Dograh-Runtime-Identity`. The original provider ID remains internal to Dograh.

The gateway remains a strict trust broker. It continues validating the exact envelope fields, agent scope, tool UUID, workflow run identity, and member bounds. It derives its stable upstream request identity from the canonical value and retains its replay, idempotency, presence-lease, and audit behavior unchanged. The Windows connector and tool schemas do not change.

Data flow:

1. The model emits an opaque tool-call ID of any nonempty length accepted by Dograh.
2. Dograh derives `tcid:v1:` plus the lowercase SHA-256 digest of the UTF-8 provider ID.
3. Dograh forwards the canonical ID with workflow, tool, and agent identity.
4. The gateway validates the bounded envelope and creates one stable tool invocation.
5. The Windows connector executes the selected opaque `app_id` and returns a structured result.

## Deployment topology

The contract is independent of machine placement. Today Dograh and the gateway run on Aptivio WSL while the native connector runs on the GPU laptop. The target co-located topology may run Dograh and the gateway in WSL/Docker on the GPU laptop with the connector native on Windows.

Co-location removes LAN addressing, remote firewall/TURN routing, cross-machine availability, and much deployment coordination. It does not remove the gateway's security role: Dograh is LLM-facing, while the connector can mutate the Windows desktop. The gateway must continue to enforce allowlists, identity, replay/idempotency, active-presence leases, and auditability. Collapsing it into Dograh is explicitly out of scope.

## Error handling and compatibility

- Missing or blank provider tool-call IDs continue to fail closed before HTTP I/O.
- Identical raw IDs produce identical canonical IDs; distinct IDs produce distinct digests for practical security purposes.
- Existing gateway validation remains unchanged and accepts the bounded canonical string.
- No raw provider ID is added to logs or forwarded headers.
- No capability membership, Windows build, or GPU deployment changes are required.
- Deployment occurs only with no active call, after backup, and with a recorded rollback path.

## Verification

Automated verification covers:

- The observed 700- and 1,268-character provider IDs.
- Exact canonical format and fixed length.
- Stable identity across retries.
- Separation of distinct calls.
- Missing-ID fail-closed behavior.
- Existing HTTP method, reserved-header, timeout, and identity tests.
- The broad Dograh API suite and gateway contract suite.

Live qualification requires one controlled GPU call:

1. Find Spotify and receive its opaque `app_id`.
2. Open exactly that `app_id`.
3. Check the last action.
4. Confirm Spotify visibly launches on the main GPU laptop.
5. Confirm no `invalid_runtime_identity`, duplicate execution, or connector degradation.
6. End the call manually and confirm the microphone is released.

Playback, Spotify authentication, `end_call`, `list_apps`, and expanded capabilities are deferred to the next feature-design session.

# Jeeves Windows Dograh Identity Propagation Design

**Status:** Approved for implementation on 2026-07-15

## Goal

Preserve Pipecat's stable `tool_call_id` across Dograh's HTTP-tool boundary so the Jeeves Windows gateway can enforce at-most-once execution, then create a dedicated, published Dograh agent for Jeeves Windows without introducing any dependency on Jeeves Deck.

## Product boundary

The only shared layer is Dograh itself:

- Dograh UI on port 3010
- Dograh API, database, Redis, TURN, and media services
- Dograh's HTTP-tool execution engine
- Dograh-level model, STT, and TTS provider configuration
- The backward-compatible identity transport implemented by this change

Jeeves Windows must not use or depend on:

- the Jeeves Deck backend, Tool Bridge, or endpoints
- the existing Deck workflow, tool records, prompts, tokens, credentials, or data
- Jeeves Deck repository code
- a new intermediary service shared by the two products

The existing `Jeeves Deck Tool Bridge Pilot` is protected state. It may be inventoried and tested for unchanged behavior, but it must not be cloned, renamed, repurposed, edited, or used as a runtime dependency.

## Dedicated Windows agent

Create a new workflow named `Jeeves Windows Voice`. It owns its own prompt, publication state, embed/session configuration, credentials, and tool records. It remains published and callable after validation.

Deck capabilities may be inventoried only as a requirements checklist. A capability is available to the Windows agent only when it has an independent Windows-owned implementation through the Jeeves Windows gateway, broker, or application. Capabilities that exist only through the Deck backend remain unavailable.

Windows tool records use an explicit `jeeves_windows` scope and a distinct naming namespace. Dograh exposes only the tools attached to the Windows workflow, and the Windows gateway independently enforces a workflow/tool allowlist. A wrong-scope or unknown tool fails closed. Agent prompts improve selection quality, but authorization never relies on prompt behavior.

## Identity transport

Dograh currently receives a stable `FunctionCallParams.tool_call_id` from Pipecat but discards it before `execute_http_tool` builds the outbound request.

The implementation will:

1. Carry the stable tool-call identity from `FunctionCallParams` into the HTTP executor.
2. Add Dograh-owned runtime identity metadata, including the workflow-run identity available on the engine.
3. Deliver identity in a reserved transport namespace that model-generated arguments cannot overwrite.
4. Preserve existing HTTP-tool behavior for tools that do not opt into the reserved metadata transport.
5. Apply the same identity semantics to voice and text-chat paths because both use the same registered HTTP-tool handler.

The exact wire shape is selected during TDD. It must be backward compatible, deterministic across retry of the same logical tool call, and validated for POST-family bodies and GET/DELETE query requests. Secrets and credentials are never included in logs or evidence.

## Deployment and rollback

The Dograh API runtime is shared even though agent objects are separate. Deployment therefore requires a controlled restart and these hard gates:

1. Record the current Dograh source SHA, running image digest, service health, and rollback command.
2. Export and hash the Deck pilot's workflow/tool configuration and record exact object IDs.
3. Confirm there are no active calls before restart.
4. Build an immutable, rollback-tagged image from the dedicated branch.
5. Restart only the required Dograh service.
6. Verify health and prove the protected Deck inventory and behavior are unchanged.
7. Roll back immediately on health failure, state drift, active-call ambiguity, or unexpected Deck behavior.

No endpoint, certificate, provider credential, network exposure, or Deck-owned object changes are authorized.

## Validation sequence

Validation proceeds from least to most stateful:

1. Unit tests proving stable propagation, reserved-field precedence, opt-in compatibility, and method-specific wire behavior.
2. Existing Dograh focused and full regression gates.
3. Image build and local/deployed health checks.
4. Deployed synthetic text and voice probes against fake Windows.
5. Retry, duplicate, timeout, cancellation, and wrong-scope probes proving the gateway can fail closed and deduplicate by stable identity.
6. Ambiguous-utterance and negative-routing tests before publication.
7. Export/diff evidence showing Deck state unchanged and the new Windows agent independently owned.

The attended real call from the installed Jeeves Windows application on the GPU laptop is a later final acceptance gate. This implementation does not install software on that laptop or exercise real Windows actions.

## Permanent bookkeeping

Dograh changes live on `feature/jeeves-windows-identity-propagation` with granular commits. The Jeeves Windows repository receives an integration record that pins:

- Dograh commit SHA and deployed image digest
- dedicated workflow/agent identity and publication state
- Windows-owned tool manifest and scope
- deployment and rollback evidence
- automated verification results
- an explicit statement that no Jeeves Deck backend dependency exists

No Dograh source is copied, symlinked, or added as a submodule to Jeeves Windows.

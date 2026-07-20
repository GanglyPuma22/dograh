# Claude CLI Handoff: Implement LAN TURN Split-Host Routing

## Mission

Implement the complete source-code and automated-verification portion of the approved LAN TURN split-host design in the existing Dograh feature worktree. The durable result must let a client receive a LAN-reachable TURN host while Dograh's server-side aiortc peer uses a separate container-reachable host, and must support an explicit coturn public/private NAT mapping without changing default behavior for existing installations.

This is a manual **local Claude CLI** handoff. Do not use OpenRouter, an API-hosted Claude model, or any nonlocal substitute.

## Why this handoff exists

The authenticated Jeeves Windows media bootstrap is working through session minting, TURN credential issuance, Cloudflare signaling, pipeline creation, and TURN allocation. A real GPU-laptop attempt failed at ICE because one `TURN_HOST` value was incorrectly serving two network audiences. Maxim approved the split-host design and wants Claude CLI to perform the implementation before Jeeves independently reviews it and authorizes the live rollout and human GPU voice test.

## Target worktree and starting point

- Worktree: `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation`
- Branch: `feature/jeeves-windows-identity-propagation`
- Required implementation baseline (must be an ancestor of HEAD): `eec358c`
- Approved design commit: `fb62608`
- Approved plan commit: `eec358c`

Verify the branch, confirm `git merge-base --is-ancestor eec358c HEAD`, and verify tracked cleanliness before editing. HEAD is expected to be later than `eec358c` because the handoff itself is committed after the plan. The existing untracked `.openclaw/` directory is unrelated user state: preserve it, do not inspect it unnecessarily, do not add it, and do not delete it.

Do not create another worktree. Work only in the named worktree.

## Requested handback

Return:

1. the implemented source changes in granular local commits;
2. focused and adjacent automated-test evidence, including red/green TDD evidence;
3. deployment/config-render evidence proving backward compatibility and the new split-host behavior;
4. a concise risk assessment and exact recommended live-rollout sequence for Jeeves to adjudicate;
5. one compact return document at:
   `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/docs/agent-handoffs/returns/claude/2026-07-19-claude-return-lan-turn-split-host.md`.

Read and follow `/home/mmounier/.openclaw/workspace/skills/coding-agent-return-handoff/SKILL.md` when preparing that return document. Create its skeleton early and update it as the run proceeds.

## Context to read first

Read these in order before editing:

1. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/docs/plans/2026-07-19-lan-turn-split-host-design.md` — approved architecture, security boundary, rollback, and acceptance criteria.
2. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/docs/plans/2026-07-19-lan-turn-split-host-implementation-plan.md` — source of truth for test-first tasks and validation; execute Tasks 1-4, then prepare (but do not perform) the operational steps in Tasks 5-6.
3. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/AGENTS.md` — repository conventions and test environment.
4. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/scripts/AGENTS.md` — Bash/PowerShell parity and deployment coupling rules.
5. `/home/mmounier/.openclaw/workspace/skills/coding-guidelines/SKILL.md` — apply these surgical-change and verification rules.
6. `/home/mmounier/.agents/vendor/superpowers/skills/executing-plans/SKILL.md`, `/home/mmounier/.agents/vendor/superpowers/skills/test-driven-development/SKILL.md`, and `/home/mmounier/.agents/vendor/superpowers/skills/verification-before-completion/SKILL.md` — use the established execution, TDD, and evidence workflow.
7. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/api/constants.py`, `api/routes/turn_credentials.py`, and `api/routes/webrtc_signaling.py` — current single-host credential and server ICE implementation.
8. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/docker-compose.yaml`, `scripts/run_dograh_init.sh`, `scripts/lib/setup_common.sh`, `scripts/setup_local.sh`, `scripts/setup_local.ps1`, and `deploy/templates/turnserver.remote.conf.template` — coupled deployment/render surfaces.
9. `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/api/tests/test_public_embed_cors.py`, `api/tests/test_public_signaling_origin.py`, and `api/tests/test_sensitive_session_logging.py` — adjacent security and compatibility coverage.
10. `/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local/docker-compose.yaml` — **read-only** reference for the current Aptivio topology, including the static coturn Docker address and the faulty live `TURN_HOST` override. Do not edit or restart this deployment.

## Current state and established diagnosis

- Jeeves Windows source/build revision `a4253db3d8b2c165d5a0a4471dd495bbed5b2363` is installed on the GPU laptop and its connector is Ready.
- Dograh source revision `5aa2a5382594054563e7281303eb6841815de9b6` plus design/plan commits `fb62608` and `eec358c` are local; nothing is pushed.
- The GPU can reach Aptivio at `10.0.0.133:3478` over TCP.
- The failed call successfully authenticated, minted a media session, returned TURN credentials, opened signaling, created the pipeline, and allocated TURN. ICE then failed and audio writes timed out.
- The client was given `host.docker.internal`; coturn advertised its Docker address. Neither is a valid remote GPU relay address.
- Approved local target configuration is conceptually:
  - client-visible `TURN_HOST=10.0.0.133`;
  - server-side `TURN_INTERNAL_HOST=host.docker.internal`;
  - coturn NAT mapping `TURN_EXTERNAL_IP=10.0.0.133/172.28.0.8`;
  - `FORCE_TURN_RELAY=true` during qualification.
- The addresses above are installation-specific examples. Implement generic optional configuration; do not hardcode them into reusable source defaults.

## Implementation scope

Execute the approved implementation plan through its source and automated-verification tasks:

1. Add an optional `TURN_INTERNAL_HOST` that defaults to `TURN_HOST`.
2. Keep public/authenticated TURN credential endpoints on `TURN_HOST`.
3. Make server-side signaling/aiortc use the internal host with the same ephemeral credential scheme.
4. Support an explicit local `TURN_EXTERNAL_IP` coturn render value, falling back to `TURN_HOST` for compatibility.
5. Pass the new settings only to the containers that require them.
6. Keep `setup_local.sh` and `setup_local.ps1` behavior and documentation in parity.
7. Add the smallest focused tests needed to prove host audience separation, fallback behavior, render behavior, Compose validity, and sensitive-log preservation.
8. Update the minimum relevant documentation described by the approved plan.
9. Prepare a sanitized, exact live-rollout recipe and rollback checklist in the return document. Do not execute them.

If the exact implementation shape in the plan conflicts with verified library or coturn behavior, stop and report the evidence rather than silently changing the approved architecture.

## Constraints and guardrails

- Test first for every behavior change. Capture the expected failing output before implementing the fix.
- Keep changes surgical; no adjacent refactors or dependency upgrades.
- Preserve existing behavior when `TURN_INTERNAL_HOST` and `TURN_EXTERNAL_IP` are absent.
- Preserve ephemeral TURN REST credentials, origin validation, header/subprotocol authentication, and sensitive-log redaction.
- Do not place credentials or session tokens in URL paths, logs, fixtures, return documents, commits, or shell history.
- Do not change the Jeeves Windows repository, gateway, workflow records, tool definitions, database contents, Dograh UI accounts, or pairing registry.
- Do not edit `/home/mmounier/.openclaw/workspace/open-source-repos/dograh-local`.
- Do not restart, recreate, or reconfigure any live container or Windows process.
- Do not ask Maxim to run a GPU test. Jeeves will review first and handle the gated rollout/test.
- Do not push, merge, rebase, tag, or open a PR.
- Do not invoke another Claude/OpenRouter/API review lane from this run.

## Suggested starting point

Start by running the worktree preflight and the currently relevant focused tests. Then write the failing host-audience tests described in Task 1 of the approved plan. Keep public URI construction in `turn_credentials.py`; add the smallest explicit host override and have only server-side signaling request the internal host. After that red/green slice is committed, move to the coturn renderer and setup-script parity.

Do not begin with live Compose edits. The live file is evidence, not a target.

## Validation and evidence requirements

At minimum, provide fresh evidence for:

- new focused public/internal TURN host tests;
- fallback behavior with no internal host configured;
- existing public embed TURN endpoint behavior;
- public signaling origin enforcement;
- sensitive session logging/redaction;
- coturn render output with and without explicit NAT mapping;
- Bash syntax and the smallest available PowerShell parse/parity check;
- `docker compose --profile local-turn config -q`;
- `git diff --check` over the exact implementation range;
- a broader relevant API suite sufficient to detect regressions in WebRTC/public embed behavior.

If the repo's canonical virtual environment or test env is unavailable, discover the nearest supported isolated command and record the deviation. Do not point tests at the live database.

Do not claim the GPU voice path is fixed: source implementation can prove readiness for rollout, but only the later gated live deployment and human microphone/duplex test can prove end-to-end success.

## Commit contract

- Make granular, traceable commits in the named Dograh worktree.
- Expected logical slices are: API host separation; coturn/config rendering; setup-script parity/docs; return packet.
- Do not squash the existing design/plan commits.
- Do not amend unrelated commits.
- Do not add `.openclaw/` or unrelated dirty state.
- Do not push.
- Record exact SHAs and subjects in the return document.

## Heartbeat contract

Create `/home/mmounier/.config/superpowers/worktrees/dograh/jeeves-windows-identity-propagation/state/runs/2026-07-19-claude-lan-turn-split-host.md` early. After each major step append one line:

```text
HH:MM <step> OK|BLOCKED <short reason>
```

Primary model: the local Claude CLI Fable model selected by Maxim. Ordered fallback: the local Claude CLI default model only if Fable is unavailable and the model switch is recorded in the heartbeat; otherwise stop. Never use OpenRouter or an API model.

The heartbeat is run state, not product source. Include it in the return references but do not commit it unless existing repository conventions explicitly require that.

## Stop and escalate rules

Stop without forcing a workaround if any of these occur:

- the worktree is not at or descended from `eec358c`;
- tracked pre-existing changes overlap the implementation files;
- tests require live credentials, the live database, or destructive state;
- coturn evidence contradicts the approved public/private mapping;
- backward compatibility would require a breaking config rename;
- implementation would require Windows/gateway changes or public Internet exposure;
- any live service mutation becomes necessary;
- a security-sensitive ambiguity cannot be resolved from the approved design and current source.

On failure or timeout, still write the return document with the last completed checkpoint, heartbeat path, exact blocker, commits already made, and validation already completed.

## Done when

The handoff is complete only when:

- all approved source implementation tasks are committed locally;
- focused and adjacent tests pass with fresh evidence;
- default single-host behavior remains covered;
- the live deployment remains untouched;
- the return document exists and identifies exact commits, files, tests, risks, and the recommended next step for Jeeves;
- the final Claude reply is path-first and stops for Jeeves review.

## Final reply format

Keep the final reply concise:

1. absolute return-document path;
2. implementation commit SHAs;
3. test totals/status;
4. explicit statement that nothing was pushed and the live deployment was not changed;
5. any blocker or remaining unproved human qualification.

# LAN TURN Split-Host Design

Approved by Maxim on 2026-07-19.

## Goal

Make the dedicated `Jeeves Windows Voice` WebRTC path work from Maxim's GPU laptop on the same private LAN as the Aptivio-hosted Dograh stack, without publishing TURN to the Internet or weakening the existing ephemeral credential and forced-relay controls.

## Diagnosed Failure

The authenticated media bootstrap, Cloudflare WebSocket signaling, Dograh pipeline creation, and TURN credential minting all succeed. ICE negotiation fails because the same `TURN_HOST` value is currently used for two different network audiences:

- the Dograh API container needs a container-reachable TURN address such as `host.docker.internal`;
- the GPU WebView needs an Aptivio LAN address such as `10.0.0.133`.

The client currently receives `host.docker.internal`, while coturn advertises its Docker address. Neither address is a usable relay candidate from the GPU laptop. The GPU has independently proven TCP reachability to `10.0.0.133:3478`, so a private-LAN relay is sufficient.

## Chosen Architecture

Introduce an optional `TURN_INTERNAL_HOST` configuration value. `TURN_HOST` remains the client-visible address returned by authenticated TURN credential endpoints. `TURN_INTERNAL_HOST` is used only by Dograh's server-side aiortc peer; it defaults to `TURN_HOST` so existing installations remain compatible.

For the Aptivio local Docker deployment:

- `TURN_HOST=10.0.0.133` is returned to the GPU client;
- `TURN_INTERNAL_HOST=host.docker.internal` is used inside the API container;
- coturn renders a NAT mapping in the form `external-ip=10.0.0.133/<coturn-container-ip>` so relayed candidates advertise the LAN address while coturn binds to its Docker interface;
- the TURN listener and relay range remain reachable only from the private LAN, with no router forwarding or public DNS exposure.

Production and ordinary single-host installs continue to omit `TURN_INTERNAL_HOST`; both peers then use `TURN_HOST` exactly as before.

## Data and Control Flow

1. Jeeves Windows requests a fresh media bootstrap through its authenticated gateway session.
2. Dograh returns ephemeral TURN credentials whose URIs use `TURN_HOST`.
3. The GPU WebView contacts the Aptivio LAN address.
4. Dograh's server-side aiortc peer builds its ICE configuration with the same ephemeral credentials but replaces only the URI host with `TURN_INTERNAL_HOST`.
5. Coturn authenticates both peers with the existing shared-secret scheme and advertises the configured LAN/Docker NAT mapping.
6. With `FORCE_TURN_RELAY=true`, SDP filtering proves audio is crossing the relay rather than succeeding through a direct candidate.

No long-lived credential is added, and no TURN token is placed in a URL path or log.

## Security Boundary

- Keep ephemeral TURN REST credentials and their current TTL.
- Keep the client-visible TURN address out of the public Cloudflare tunnel.
- Limit TCP/UDP 3478, TCP/UDP 5349, and UDP 49152-49200 to the private LAN/private Windows firewall profile.
- Do not add router forwarding, public DNS, or a public coturn listener.
- Preserve origin validation, authenticated media bootstrap ownership, and sanitized request logging.
- Do not log TURN passwords, session tokens, or the Dograh UI password-reset credential.

## Failure Handling and Rollback

Configuration validation must reject an empty explicitly supplied internal host and must leave existing deployments unchanged when it is absent. If qualification fails, restore the captured compose/env/generated-coturn configuration and restart only the affected Dograh API/init/coturn services. The Windows build, pairing registry, workflow definition, tool definitions, and Jeeves Deck objects are outside this change.

## Verification

The correction is accepted only when all of the following pass:

- unit tests prove public credentials use `TURN_HOST` and server-side ICE uses `TURN_INTERNAL_HOST`;
- fallback tests prove an absent internal host preserves current behavior;
- render tests prove the local coturn NAT mapping is emitted without changing remote production output;
- existing public-embed, sensitive-logging, and signaling tests remain green;
- generated compose/config inspection contains no secret-bearing request path;
- the GPU completes a forced-relay voice call with microphone audio and spoken response;
- ending the call releases the Windows microphone indicator and closes the Dograh pipeline;
- container restart counts and gateway connector health remain stable.

## Rejected Alternatives

### Split-DNS or hosts-file alias

Using one hostname that resolves differently inside Docker and on the GPU is a smaller operational patch, but it depends on machine-specific hosts files or DNS state and makes future debugging ambiguous.

### Public or managed TURN

A public relay would support off-LAN clients but adds attack surface, firewall/router work, and possibly recurring cost. It is unnecessary for the currently approved same-LAN GPU qualification.

### Disable forced relay

Allowing direct ICE might hide the broken relay and produce a misleading success. Forced relay remains enabled until the TURN path is proven end to end.

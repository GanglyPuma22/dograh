"""Tests for public/internal TURN host audience separation.

`TURN_HOST` is the client-visible relay address returned by the TURN
credential endpoints. `TURN_INTERNAL_HOST` is used only by Dograh's
server-side aiortc peer (a Docker container may need a different route to
coturn than a LAN client does) and defaults to `TURN_HOST` so single-host
installations keep their current behavior.
"""

import importlib
import re

import pytest

CLIENT_HOST = "10.0.0.133"
INTERNAL_HOST = "host.docker.internal"
TEST_ONLY_SECRET = "unit-test-secret"


@pytest.fixture
def reload_turn_modules(monkeypatch):
    """Reload the TURN modules under a controlled env, restoring them after.

    The TURN settings are read at import time, so each scenario reloads
    api.constants and the two route modules that consume them.
    """

    import api.constants
    import api.routes.turn_credentials
    import api.routes.webrtc_signaling

    def _reload(**env):
        for key in ("TURN_HOST", "TURN_INTERNAL_HOST", "TURN_SECRET"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        importlib.reload(api.constants)
        importlib.reload(api.routes.turn_credentials)
        importlib.reload(api.routes.webrtc_signaling)
        return api.constants, api.routes.turn_credentials, api.routes.webrtc_signaling

    yield _reload

    monkeypatch.undo()
    importlib.reload(api.constants)
    importlib.reload(api.routes.turn_credentials)
    importlib.reload(api.routes.webrtc_signaling)


def _turn_servers(ice_servers):
    """Return the non-STUN ICE server entries."""
    return [
        server
        for server in ice_servers
        if not str(server.urls).startswith("stun:")
    ]


def test_public_credentials_use_client_visible_host(reload_turn_modules):
    _, turn_credentials, _ = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_INTERNAL_HOST=INTERNAL_HOST,
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    public = turn_credentials.generate_turn_credentials("public-user")

    assert public["uris"]
    assert all(CLIENT_HOST in uri for uri in public["uris"])
    assert not any(INTERNAL_HOST in uri for uri in public["uris"])


def test_internal_credentials_use_internal_host(reload_turn_modules):
    constants, turn_credentials, _ = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_INTERNAL_HOST=INTERNAL_HOST,
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    internal = turn_credentials.generate_turn_credentials(
        "server-user", host=constants.TURN_INTERNAL_HOST
    )

    assert internal["uris"]
    assert all(INTERNAL_HOST in uri for uri in internal["uris"])
    assert not any(CLIENT_HOST in uri for uri in internal["uris"])
    # The ephemeral credential scheme is unchanged by the host override.
    assert internal["username"].endswith(":server-user")
    assert internal["password"]


def test_internal_host_defaults_to_turn_host_when_absent(reload_turn_modules):
    constants, turn_credentials, _ = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    assert constants.TURN_INTERNAL_HOST == CLIENT_HOST

    defaulted = turn_credentials.generate_turn_credentials(
        "server-user", host=constants.TURN_INTERNAL_HOST
    )
    public = turn_credentials.generate_turn_credentials("server-user")

    assert defaulted["uris"] == public["uris"]
    assert all(CLIENT_HOST in uri for uri in public["uris"])


def test_empty_internal_host_falls_back_to_turn_host(reload_turn_modules):
    # docker-compose passes TURN_INTERNAL_HOST as "${TURN_INTERNAL_HOST:-}",
    # so existing installs inject an empty string rather than unsetting it.
    constants, _, _ = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_INTERNAL_HOST="",
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    assert constants.TURN_INTERNAL_HOST == CLIENT_HOST


def test_server_side_ice_servers_use_internal_host(reload_turn_modules):
    _, _, webrtc_signaling = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_INTERNAL_HOST=INTERNAL_HOST,
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    turn_servers = _turn_servers(webrtc_signaling.get_ice_servers(user_id="42"))

    assert turn_servers, "expected a TURN entry in the server-side ICE config"
    for server in turn_servers:
        assert all(INTERNAL_HOST in url for url in server.urls)
        assert not any(CLIENT_HOST in url for url in server.urls)
        assert re.fullmatch(r"\d+:42", server.username)
        assert server.credential


def test_server_side_ice_servers_keep_turn_host_without_internal_host(
    reload_turn_modules,
):
    _, _, webrtc_signaling = reload_turn_modules(
        TURN_HOST=CLIENT_HOST,
        TURN_SECRET=TEST_ONLY_SECRET,
    )

    turn_servers = _turn_servers(webrtc_signaling.get_ice_servers(user_id="42"))

    assert turn_servers, "expected a TURN entry in the server-side ICE config"
    for server in turn_servers:
        assert all(CLIENT_HOST in url for url in server.urls)

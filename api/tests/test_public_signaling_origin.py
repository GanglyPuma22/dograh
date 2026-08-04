"""Tests for public WebRTC signaling allowed-domain enforcement.

Regression for issue #330: the public signaling WebSocket
(`/public/signaling/{session_token}`) must enforce the embed token's
allowed-domain policy, mirroring the HTTP embed endpoints. Before the fix it
validated only the session token and expiry, so a leaked or replayed session
token could attach to the signaling path from an arbitrary origin.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

SESSION_TOKEN = "emb_session_0123456789abcdefghijklmnopqrstuv"
MEDIA_PROTOCOL = "jeeves-media-v1"


class _FakeWebSocket:
    """Minimal WebSocket double exposing handshake headers and close()."""

    def __init__(self, origin: str, protocols: str | None = None):
        self.headers = {"origin": origin}
        if protocols is not None:
            self.headers["sec-websocket-protocol"] = protocols
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.receive_json = AsyncMock(side_effect=Exception("stop"))


def _embed_session():
    return SimpleNamespace(expires_at=None, embed_token_id=1, workflow_run_id=42)


def _embed_token(allowed_domains):
    return SimpleNamespace(allowed_domains=allowed_domains, created_by=7, workflow_id=3)


def _patch_deps(session=None):
    """Patch db_client + signaling_manager for a valid, non-expired session."""
    db = patch("api.routes.webrtc_signaling.db_client").start()
    mgr = patch("api.routes.webrtc_signaling.signaling_manager").start()
    db.get_embed_session_by_token = AsyncMock(return_value=session or _embed_session())
    db.get_embed_token_by_id = AsyncMock(return_value=_embed_token(["example.com"]))
    db.get_user_by_id = AsyncMock(return_value=SimpleNamespace(id=7))
    mgr.handle_websocket = AsyncMock()
    return db, mgr


@pytest.mark.asyncio
async def test_public_signaling_rejects_disallowed_origin():
    from api.routes.webrtc_signaling import public_signaling_websocket

    ws = _FakeWebSocket("https://evil.example")
    _db, mgr = _patch_deps()
    try:
        await public_signaling_websocket(ws, "emb_session_tok")
    finally:
        patch.stopall()

    # Regression (issue #330): a valid session token presented from an origin
    # outside the embed allowlist must be rejected before the signaling handoff.
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008
    mgr.handle_websocket.assert_not_called()


@pytest.mark.asyncio
async def test_public_signaling_accepts_allowed_origin():
    from api.routes.webrtc_signaling import public_signaling_websocket

    ws = _FakeWebSocket("https://example.com")
    _db, mgr = _patch_deps()
    try:
        await public_signaling_websocket(ws, "emb_session_tok")
    finally:
        patch.stopall()

    # An origin within the allowlist proceeds to the signaling handoff.
    mgr.handle_websocket.assert_awaited_once()


@pytest.mark.asyncio
async def test_credential_free_public_signaling_authenticates_and_echoes_only_fixed_protocol():
    from api.routes.webrtc_signaling import public_signaling_websocket_from_protocol

    ws = _FakeWebSocket("https://example.com", f"{MEDIA_PROTOCOL}, {SESSION_TOKEN}")
    db, mgr = _patch_deps()
    try:
        await public_signaling_websocket_from_protocol(ws)
    finally:
        patch.stopall()

    db.get_embed_session_by_token.assert_awaited_once_with(SESSION_TOKEN)
    mgr.handle_websocket.assert_awaited_once()
    assert mgr.handle_websocket.await_args.kwargs == {
        "accepted_subprotocol": MEDIA_PROTOCOL
    }
    assert SESSION_TOKEN not in "/api/v1/ws/public/signaling"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocols",
    [
        None,
        MEDIA_PROTOCOL,
        f"wrong-protocol, {SESSION_TOKEN}",
        f"{MEDIA_PROTOCOL}, invalid-token",
        f"{MEDIA_PROTOCOL}, {SESSION_TOKEN}, extra",
    ],
)
async def test_credential_free_public_signaling_rejects_missing_or_malformed_protocols(
    protocols,
):
    from api.routes.webrtc_signaling import public_signaling_websocket_from_protocol

    ws = _FakeWebSocket("https://example.com", protocols)
    db, mgr = _patch_deps()
    try:
        await public_signaling_websocket_from_protocol(ws)
    finally:
        patch.stopall()

    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 1008
    db.get_embed_session_by_token.assert_not_called()
    mgr.handle_websocket.assert_not_called()


@pytest.mark.asyncio
async def test_credential_free_public_signaling_enforces_expiry_and_origin():
    from api.routes.webrtc_signaling import public_signaling_websocket_from_protocol

    expired = SimpleNamespace(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        embed_token_id=1,
        workflow_run_id=42,
    )
    expired_ws = _FakeWebSocket(
        "https://example.com", f"{MEDIA_PROTOCOL}, {SESSION_TOKEN}"
    )
    _db, mgr = _patch_deps(expired)
    try:
        await public_signaling_websocket_from_protocol(expired_ws)
    finally:
        patch.stopall()
    expired_ws.close.assert_awaited_once()
    mgr.handle_websocket.assert_not_called()

    denied_ws = _FakeWebSocket(
        "https://evil.example", f"{MEDIA_PROTOCOL}, {SESSION_TOKEN}"
    )
    _db, mgr = _patch_deps()
    try:
        await public_signaling_websocket_from_protocol(denied_ws)
    finally:
        patch.stopall()
    denied_ws.close.assert_awaited_once()
    mgr.handle_websocket.assert_not_called()


@pytest.mark.asyncio
async def test_signaling_manager_echoes_only_the_fixed_protocol():
    from starlette.websockets import WebSocketDisconnect

    from api.routes.webrtc_signaling import SignalingManager

    ws = _FakeWebSocket("https://example.com")
    ws.receive_json = AsyncMock(side_effect=WebSocketDisconnect())
    manager = SignalingManager()

    await manager.handle_websocket(
        ws,
        workflow_id=3,
        workflow_run_id=42,
        user=SimpleNamespace(id=7),
        accepted_subprotocol=MEDIA_PROTOCOL,
    )

    ws.accept.assert_awaited_once_with(subprotocol=MEDIA_PROTOCOL)

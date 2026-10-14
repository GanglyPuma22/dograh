import ast
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.routes import game_companion
from api.services.game_companion.protocol import (
    MAX_BINARY_FRAME_BYTES,
    MAX_JSON_BYTES,
    AudioEnd,
    AudioStart,
    Caption,
    State,
)
from test_game_companion_session import FakeLLM, FakeSTT, FakeTTS, fake_providers
from api.services.game_companion.providers import LLMResult, PCMChunk
from api.services.game_companion.session import CompanionSession
import asyncio

HELLO = {
    "type": "hello",
    "protocol_version": 1,
    "client": "salvage",
    "save_id": "phase_2_prototype",
    "capabilities": ["pcm_s16le", "captions", "tools", "memory"],
}
COMPANION_TOKEN = "local-test-token"


class RecordingSession:
    def __init__(self, emit):
        self.emit = emit
        self.active_turn_id = None
        self.started = []
        self.audio = []
        self.ended = []
        self.interrupted = []
        self.tool_results = []
        self.memory_results = []
        self.client_capabilities = None
        self.closed = False

    def set_client_capabilities(self, capabilities):
        self.client_capabilities = frozenset(capabilities)

    async def start_turn(self, turn_id, context):
        self.active_turn_id = turn_id
        self.started.append((turn_id, context))
        await self.emit(State(type="state", turn_id=turn_id, state="listening"))

    async def append_audio(self, turn_id, audio):
        self.audio.append((turn_id, audio))

    async def end_turn(self, turn_id):
        self.ended.append(turn_id)
        await self.emit(
            Caption(
                type="caption",
                turn_id=turn_id,
                speaker="player",
                text="Take me to the moon",
                final=True,
            )
        )
        await self.emit(
            AudioStart(
                type="audio_start",
                turn_id=turn_id,
                sample_rate=24000,
                channels=1,
                format="pcm_s16le",
            )
        )
        await self.emit(b"\x01\x00\x02\x00")
        await self.emit(AudioEnd(type="audio_end", turn_id=turn_id))

    async def submit_tool_result(self, result):
        self.tool_results.append(result)
        await self.emit(State(type="state", turn_id=result.turn_id, state="thinking"))

    async def submit_memory_result(self, result):
        self.memory_results.append(result)
        await self.emit(State(type="state", turn_id=result.turn_id, state="thinking"))

    async def interrupt(self, turn_id):
        self.interrupted.append(turn_id)
        self.active_turn_id = None

    async def close(self):
        self.closed = True


class OversizedOutputSession(RecordingSession):
    async def end_turn(self, turn_id):
        await self.emit(
            AudioStart(
                type="audio_start",
                turn_id=turn_id,
                sample_rate=24000,
                channels=1,
                format="pcm_s16le",
            )
        )
        await self.emit(b"x" * (MAX_BINARY_FRAME_BYTES + 1))


@pytest.fixture
def enabled_client(monkeypatch):
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    sessions = []

    def factory(emit):
        session = RecordingSession(emit)
        sessions.append(session)
        return session

    monkeypatch.setattr(game_companion, "create_companion_session", factory)
    app = FastAPI()
    app.include_router(game_companion.router)
    with TestClient(app) as client:
        yield client, sessions


def connect_and_hello(client):
    websocket = client.websocket_connect(
        "/game-companion/ws",
        headers={"Authorization": f"Bearer {COMPANION_TOKEN}"},
    )
    connection = websocket.__enter__()
    connection.send_json(HELLO)
    acknowledgement = connection.receive_json()
    assert acknowledgement["type"] == "hello_ack"
    assert acknowledgement["protocol_version"] == 1
    return websocket, connection, acknowledgement


@pytest.mark.parametrize("event,context", [
    ("aster_first_wake", {}),
    ("aster_first_wake", {"companion_wake": {"activated": 1, "phase": "awake", "first_activation_this_session": True}}),
    ("invented_event", {}),
    ("station_assay_complete", {}),
])
def test_invalid_event_does_not_close_or_interrupt_active_speech(monkeypatch, event, context):
    sessions = []
    holders = []

    class HeldTTS(FakeTTS):
        async def synthesize(self, text):
            yield PCMChunk(audio=b"\x01\x00", sample_rate=24000)
            hold = asyncio.Event()
            holders.append(hold)
            await hold.wait()

    def factory(emit):
        session = CompanionSession(providers=fake_providers(tts=HeldTTS()), emit=emit)
        sessions.append(session)
        return session

    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    monkeypatch.setattr(game_companion, "create_companion_session", factory)
    app = FastAPI()
    app.include_router(game_companion.router)
    with TestClient(app) as client:
        manager, connection, ack = connect_and_hello(client)
        try:
            assert set(ack["capabilities"]) == {"game_events_v1", "aster_wake_v1"}
            connection.send_json({"type": "turn_start", "turn_id": "speaking", "context": {}})
            connection.send_bytes(b"\x00\x00")
            connection.send_json({"type": "turn_end", "turn_id": "speaking"})
            while connection.receive_json()["type"] != "audio_start":
                pass
            assert connection.receive_bytes() == b"\x01\x00"
            connection.send_json({"type": "game_event", "turn_id": "bad-event", "event": event, "context": context})
            error = connection.receive_json()
            assert error["type"] == "error" and error["recoverable"] is True
            assert error["turn_id"] == "bad-event"
            assert sessions[0].active_turn_id == "speaking"
            assert not sessions[0].cancelled_turn_ids
            connection.send_json({"type": "interrupt", "turn_id": "speaking"})
            connection.send_json({"type": "turn_start", "turn_id": "reply", "context": {}})
            assert connection.receive_json()["state"] == "listening"
        finally:
            manager.__exit__(None, None, None)


def test_authenticated_wake_event_streams_without_microphone_then_accepts_player_reply(monkeypatch):
    stt, llm, tts = FakeSTT(), FakeLLM([
        LLMResult(text="Hello. I'm Aster."), LLMResult(text="Hello to you too."),
    ]), FakeTTS()
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    monkeypatch.setattr(game_companion, "create_companion_session", lambda emit: CompanionSession(
        providers=fake_providers(stt=stt, llm=llm, tts=tts), emit=emit,
    ))
    app = FastAPI()
    app.include_router(game_companion.router)
    with TestClient(app) as client:
        manager, connection, ack = connect_and_hello(client)
        try:
            assert "aster_wake_v1" in ack["capabilities"]
            connection.send_json({"type": "game_event", "turn_id": "wake", "event": "aster_first_wake", "context": {
                "companion_wake": {"activated": True, "phase": "awake", "first_activation_this_session": True},
            }})
            captions = []
            while True:
                frame = connection.receive_json()
                if frame["type"] == "caption":
                    captions.append(frame)
                if frame["type"] == "audio_start":
                    assert connection.receive_bytes() == b"\x01\x00\x02\x00"
                if frame.get("state") == "idle":
                    break
            assert [(c["speaker"], c["text"]) for c in captions] == [("assistant", "Hello. I'm Aster.")]
            assert not stt.calls and llm.calls[0]["tools"] == []
            connection.send_json({"type": "turn_start", "turn_id": "reply", "context": {}})
            assert connection.receive_json()["state"] == "listening"
        finally:
            manager.__exit__(None, None, None)


def test_route_is_disabled_without_explicit_local_opt_in(monkeypatch):
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", False)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", "")
    app = FastAPI()
    app.include_router(game_companion.router)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect("/game-companion/ws"),
    ):
        pass

    assert disconnect.value.code == 1008


def test_route_rejects_missing_companion_token_before_session_creation(monkeypatch):
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    created = []
    monkeypatch.setattr(
        game_companion,
        "create_companion_session",
        lambda emit: created.append(emit),
    )
    app = FastAPI()
    app.include_router(game_companion.router)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect("/game-companion/ws"),
    ):
        pass

    assert disconnect.value.code == 1008
    assert created == []


def test_route_rejects_malformed_bearer_authorization_without_crashing(monkeypatch):
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    created = []
    monkeypatch.setattr(
        game_companion,
        "create_companion_session",
        lambda emit: created.append(emit),
    )
    app = FastAPI()
    app.include_router(game_companion.router)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect(
            "/game-companion/ws",
            headers={"Authorization": "Bearer"},
        ),
    ):
        pass

    assert disconnect.value.code == 1008
    assert created == []


def test_route_rejects_companion_token_in_logged_query_string(monkeypatch):
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_ENABLED", True)
    monkeypatch.setattr(game_companion, "DOGRAH_GAME_COMPANION_TOKEN", COMPANION_TOKEN)
    created = []
    monkeypatch.setattr(
        game_companion,
        "create_companion_session",
        lambda emit: created.append(emit),
    )
    app = FastAPI()
    app.include_router(game_companion.router)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect(f"/game-companion/ws?token={COMPANION_TOKEN}"),
    ):
        pass

    assert disconnect.value.code == 1008
    assert created == []


def test_route_session_uses_selected_game_companion_provider_factory(monkeypatch):
    selected_providers = object()
    monkeypatch.setattr(
        game_companion,
        "create_game_companion_provider_set",
        lambda: selected_providers,
    )

    async def emit(_event):
        pass

    session = game_companion.create_companion_session(emit)

    assert session.providers is selected_providers


def test_hello_negotiates_protocol_and_audio(enabled_client):
    client, sessions = enabled_client
    websocket, _connection, acknowledgement = connect_and_hello(client)
    websocket.__exit__(None, None, None)

    assert acknowledgement["companion"] == "Aster"
    assert acknowledgement["session_id"]
    assert acknowledgement["audio"] == {
        "sample_rate": 16000,
        "channels": 1,
        "format": "pcm_s16le",
    }
    assert len(sessions) == 1
    assert sessions[0].client_capabilities == frozenset(HELLO["capabilities"])
    assert sessions[0].closed is True


def test_ordered_audio_and_binary_response_are_delegated(enabled_client):
    client, sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)

    connection.send_json(
        {"type": "turn_start", "turn_id": "turn-1", "context": {"mode": "ship"}}
    )
    assert connection.receive_json() == {
        "type": "state",
        "turn_id": "turn-1",
        "state": "listening",
    }
    connection.send_bytes(b"\x00\x00\x01\x00")
    connection.send_json({"type": "turn_end", "turn_id": "turn-1"})

    assert connection.receive_json()["type"] == "caption"
    assert connection.receive_json()["type"] == "audio_start"
    assert connection.receive_bytes() == b"\x01\x00\x02\x00"
    assert connection.receive_json()["type"] == "audio_end"
    websocket.__exit__(None, None, None)

    assert sessions[0].started == [("turn-1", {"mode": "ship"})]
    assert sessions[0].audio == [("turn-1", b"\x00\x00\x01\x00")]
    assert sessions[0].ended == ["turn-1"]


def test_interrupt_is_forwarded_and_connection_accepts_a_new_turn(enabled_client):
    client, sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)
    connection.send_json({"type": "turn_start", "turn_id": "old", "context": {}})
    assert connection.receive_json()["state"] == "listening"
    connection.send_json({"type": "interrupt", "turn_id": "old"})
    connection.send_json({"type": "turn_start", "turn_id": "new", "context": {}})
    assert connection.receive_json() == {
        "type": "state",
        "turn_id": "new",
        "state": "listening",
    }
    websocket.__exit__(None, None, None)

    assert sessions[0].interrupted == ["old"]
    assert [turn_id for turn_id, _context in sessions[0].started] == ["old", "new"]


def test_typed_tool_result_is_forwarded_after_turn_end(enabled_client):
    client, sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)
    connection.send_json({"type": "turn_start", "turn_id": "turn-1", "context": {}})
    assert connection.receive_json()["state"] == "listening"
    connection.send_json({"type": "turn_end", "turn_id": "turn-1"})
    assert connection.receive_json()["type"] == "caption"
    assert connection.receive_json()["type"] == "audio_start"
    assert connection.receive_bytes()
    assert connection.receive_json()["type"] == "audio_end"

    connection.send_json(
        {
            "type": "tool_result",
            "turn_id": "turn-1",
            "call_id": "call-1",
            "ok": True,
            "result": {"body_id": "planet_01_moon"},
        }
    )
    assert connection.receive_json()["state"] == "thinking"
    websocket.__exit__(None, None, None)

    assert sessions[0].tool_results[0].call_id == "call-1"


def test_typed_memory_result_is_forwarded_after_turn_end(enabled_client):
    client, sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)
    connection.send_json({"type": "turn_start", "turn_id": "turn-1", "context": {}})
    assert connection.receive_json()["state"] == "listening"
    connection.send_json({"type": "turn_end", "turn_id": "turn-1"})
    assert connection.receive_json()["type"] == "caption"
    assert connection.receive_json()["type"] == "audio_start"
    assert connection.receive_bytes()
    assert connection.receive_json()["type"] == "audio_end"

    connection.send_json(
        {
            "type": "memory_result",
            "turn_id": "turn-1",
            "query_id": "query-1",
            "ok": True,
            "records": [{"event_id": "event-1", "event_type": "session_started"}],
        }
    )
    assert connection.receive_json()["state"] == "thinking"
    websocket.__exit__(None, None, None)

    result = sessions[0].memory_results[0]
    assert result.query_id == "query-1"
    assert result.records == [{"event_id": "event-1", "event_type": "session_started"}]


def test_binary_before_turn_start_is_rejected(enabled_client):
    client, _sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)

    connection.send_bytes(b"\x00\x00")
    with pytest.raises(WebSocketDisconnect) as disconnect:
        connection.receive_json()
    websocket.__exit__(None, None, None)

    assert disconnect.value.code == 1008


def test_oversized_json_is_rejected_before_parsing(enabled_client):
    client, _sessions = enabled_client
    websocket, connection, _acknowledgement = connect_and_hello(client)
    oversized = json.dumps(
        {
            "type": "turn_start",
            "turn_id": "turn-1",
            "context": {"padding": "x" * MAX_JSON_BYTES},
        }
    )

    connection.send_text(oversized)
    with pytest.raises(WebSocketDisconnect) as disconnect:
        connection.receive_json()
    websocket.__exit__(None, None, None)

    assert disconnect.value.code == 1009


def test_oversized_outbound_binary_frame_is_rejected(enabled_client, monkeypatch):
    client, sessions = enabled_client

    def factory(emit):
        session = OversizedOutputSession(emit)
        sessions.append(session)
        return session

    monkeypatch.setattr(game_companion, "create_companion_session", factory)
    websocket, connection, _acknowledgement = connect_and_hello(client)
    connection.send_json({"type": "turn_start", "turn_id": "turn-1", "context": {}})
    assert connection.receive_json()["state"] == "listening"
    connection.send_json({"type": "turn_end", "turn_id": "turn-1"})
    assert connection.receive_json()["type"] == "audio_start"

    try:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            connection.receive_bytes()
    finally:
        websocket.__exit__(None, None, None)

    assert disconnect.value.code == 1009


def test_main_router_mounts_companion_router():
    source = (Path(__file__).parents[1] / "routes" / "main.py").read_text()
    tree = ast.parse(source)

    imported_aliases = {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "api.routes.game_companion"
        for alias in node.names
        if alias.name == "router"
    }
    mounted_names = {
        call.args[0].id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "include_router"
        and call.args
        and isinstance(call.args[0], ast.Name)
    }

    assert "game_companion_router" in imported_aliases
    assert "game_companion_router" in mounted_names

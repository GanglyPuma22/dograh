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

HELLO = {
    "type": "hello",
    "protocol_version": 1,
    "client": "salvage",
    "save_id": "phase_2_prototype",
    "capabilities": ["pcm_s16le", "captions", "tools", "memory"],
}


class RecordingSession:
    def __init__(self, emit):
        self.emit = emit
        self.active_turn_id = None
        self.started = []
        self.audio = []
        self.ended = []
        self.interrupted = []
        self.tool_results = []
        self.closed = False

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
    monkeypatch.setenv("DOGRAH_GAME_COMPANION_ENABLED", "1")
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
    websocket = client.websocket_connect("/game-companion/ws")
    connection = websocket.__enter__()
    connection.send_json(HELLO)
    acknowledgement = connection.receive_json()
    assert acknowledgement["type"] == "hello_ack"
    assert acknowledgement["protocol_version"] == 1
    return websocket, connection, acknowledgement


def test_route_is_disabled_without_explicit_local_opt_in(monkeypatch):
    monkeypatch.delenv("DOGRAH_GAME_COMPANION_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(game_companion.router)

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect("/game-companion/ws"),
    ):
        pass

    assert disconnect.value.code == 1008


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

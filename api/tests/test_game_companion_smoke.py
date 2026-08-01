from fastapi.testclient import TestClient

from api.tools.game_companion_smoke import (
    AUTHORITY_INPUT_CHUNKS,
    AUTHORITY_OUTPUT_CHUNKS,
    AUTHORITY_TURN_ID,
    BODY_ID,
    FINAL_ASSISTANT_CAPTION,
    INTERRUPT_INPUT_CHUNKS,
    INTERRUPT_OUTPUT_CHUNK,
    INTERRUPT_TURN_ID,
    MEMORY_QUERY_ID,
    NAVIGATION_CALL_ID,
    SMOKE_MEMORY_RECORD,
    SmokeCoordinator,
    create_smoke_app,
)

HELLO = {
    "type": "hello",
    "protocol_version": 1,
    "client": "salvage",
    "save_id": "smoke-save",
    "capabilities": ["pcm_s16le", "captions", "tools", "memory"],
}


def _expect_json(connection, expected: dict) -> dict:
    message = connection.receive_json()
    for key, value in expected.items():
        assert message[key] == value
    return message


def test_smoke_health_is_provider_free():
    app = create_smoke_app()

    with TestClient(app) as client:
        response = client.get("/smoke/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "providers": "fake"}


def test_smoke_app_drives_interrupt_authority_memory_and_cleanup():
    coordinator = SmokeCoordinator()
    app = create_smoke_app(coordinator)

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/game-companion/ws") as connection:
            connection.send_json(HELLO)
            acknowledgement = _expect_json(
                connection,
                {
                    "type": "hello_ack",
                    "protocol_version": 1,
                    "companion": "Aster",
                },
            )
            assert acknowledgement["session_id"]
            assert acknowledgement["audio"] == {
                "sample_rate": 16000,
                "channels": 1,
                "format": "pcm_s16le",
            }

            connection.send_json(
                {
                    "type": "turn_start",
                    "turn_id": INTERRUPT_TURN_ID,
                    "context": {"mode": "ship"},
                }
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": INTERRUPT_TURN_ID,
                    "state": "listening",
                },
            )
            for chunk in INTERRUPT_INPUT_CHUNKS:
                connection.send_bytes(chunk)
            connection.send_json({"type": "turn_end", "turn_id": INTERRUPT_TURN_ID})
            _expect_json(
                connection,
                {
                    "type": "caption",
                    "turn_id": INTERRUPT_TURN_ID,
                    "speaker": "player",
                    "text": "interrupt me",
                    "final": True,
                },
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": INTERRUPT_TURN_ID,
                    "state": "thinking",
                },
            )
            _expect_json(
                connection,
                {
                    "type": "caption",
                    "turn_id": INTERRUPT_TURN_ID,
                    "speaker": "assistant",
                    "final": True,
                },
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": INTERRUPT_TURN_ID,
                    "state": "speaking",
                },
            )
            _expect_json(
                connection,
                {
                    "type": "audio_start",
                    "turn_id": INTERRUPT_TURN_ID,
                    "sample_rate": 24000,
                    "channels": 1,
                    "format": "pcm_s16le",
                },
            )
            assert connection.receive_bytes() == INTERRUPT_OUTPUT_CHUNK

            connection.send_json({"type": "interrupt", "turn_id": INTERRUPT_TURN_ID})
            connection.send_json(
                {
                    "type": "turn_start",
                    "turn_id": AUTHORITY_TURN_ID,
                    "context": {
                        "mode": "ship",
                        "current_body_id": "planet_01",
                        "target_body_id": "",
                    },
                }
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": AUTHORITY_TURN_ID,
                    "state": "listening",
                },
            )
            for chunk in AUTHORITY_INPUT_CHUNKS:
                connection.send_bytes(chunk)
            connection.send_json({"type": "turn_end", "turn_id": AUTHORITY_TURN_ID})
            _expect_json(
                connection,
                {
                    "type": "caption",
                    "turn_id": AUTHORITY_TURN_ID,
                    "speaker": "player",
                    "text": "set course and recall",
                    "final": True,
                },
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": AUTHORITY_TURN_ID,
                    "state": "thinking",
                },
            )
            _expect_json(
                connection,
                {
                    "type": "tool_call",
                    "turn_id": AUTHORITY_TURN_ID,
                    "call_id": NAVIGATION_CALL_ID,
                    "name": "set_navigation_target",
                    "arguments": {"body_id": BODY_ID},
                },
            )
            _expect_json(
                connection,
                {
                    "type": "memory_query",
                    "turn_id": AUTHORITY_TURN_ID,
                    "query_id": MEMORY_QUERY_ID,
                    "name": "recent_activity",
                    "arguments": {"limit": 1},
                },
            )
            connection.send_json(
                {
                    "type": "tool_result",
                    "turn_id": AUTHORITY_TURN_ID,
                    "call_id": NAVIGATION_CALL_ID,
                    "ok": True,
                    "result": {
                        "body_id": BODY_ID,
                        "navigation_state": {
                            "target_body_id": BODY_ID,
                            "status": "targeted",
                        },
                    },
                }
            )
            connection.send_json(
                {
                    "type": "memory_result",
                    "turn_id": AUTHORITY_TURN_ID,
                    "query_id": MEMORY_QUERY_ID,
                    "ok": True,
                    "records": [SMOKE_MEMORY_RECORD],
                }
            )
            _expect_json(
                connection,
                {
                    "type": "caption",
                    "turn_id": AUTHORITY_TURN_ID,
                    "speaker": "assistant",
                    "text": FINAL_ASSISTANT_CAPTION,
                    "final": True,
                },
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": AUTHORITY_TURN_ID,
                    "state": "speaking",
                },
            )
            _expect_json(
                connection,
                {
                    "type": "audio_start",
                    "turn_id": AUTHORITY_TURN_ID,
                    "sample_rate": 24000,
                    "channels": 1,
                    "format": "pcm_s16le",
                },
            )
            for chunk in AUTHORITY_OUTPUT_CHUNKS:
                assert connection.receive_bytes() == chunk
            _expect_json(
                connection,
                {"type": "audio_end", "turn_id": AUTHORITY_TURN_ID},
            )
            _expect_json(
                connection,
                {
                    "type": "state",
                    "turn_id": AUTHORITY_TURN_ID,
                    "state": "idle",
                },
            )

        status = client.get("/smoke/status").json()

    assert status == {
        "sessions_created": 1,
        "sessions_closed": 1,
        "stt_turns": 2,
        "ordered_pcm_validated": True,
        "interrupted_tts_cancelled": True,
        "tool_result_validated": True,
        "memory_result_validated": True,
        "provider_close_counts": {"stt": 1, "llm": 1, "tts": 1},
        "failures": [],
    }

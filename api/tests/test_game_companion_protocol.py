import pytest

from api.services.game_companion.protocol import (
    AnnotationProposal,
    ClientMessageOrder,
    Hello,
    MemoryResult,
    ProtocolError,
    ToolCall,
    ToolResult,
    TurnEnd,
    TurnStart,
    parse_client_message,
    parse_server_message,
)


def hello_message(**overrides):
    message = {
        "type": "hello",
        "protocol_version": 1,
        "client": "salvage",
        "save_id": "phase_2_prototype",
        "capabilities": ["pcm_s16le", "captions", "tools", "memory"],
    }
    message.update(overrides)
    return message


def test_hello_requires_protocol_v1():
    with pytest.raises(ProtocolError, match="unsupported_protocol"):
        parse_client_message({"type": "hello", "protocol_version": 2})


def test_hello_ignores_unknown_fields_for_forward_compatibility():
    message = parse_client_message(hello_message(future_capability_metadata=True))

    assert isinstance(message, Hello)
    assert message.protocol_version == 1
    assert not hasattr(message, "future_capability_metadata")


def test_turn_scoped_message_requires_turn_id():
    with pytest.raises(ProtocolError, match="turn_id"):
        parse_client_message({"type": "turn_end"})


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {"type": "turn_start", "turn_id": "turn-1", "context": {}},
            TurnStart,
        ),
        ({"type": "turn_end", "turn_id": "turn-1"}, TurnEnd),
        ({"type": "interrupt", "turn_id": "turn-1"}, object),
        (
            {
                "type": "tool_result",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "ok": True,
                "result": {"body_id": "planet_01_moon"},
            },
            ToolResult,
        ),
        (
            {
                "type": "memory_result",
                "turn_id": "turn-1",
                "query_id": "query-1",
                "ok": True,
                "records": [],
            },
            MemoryResult,
        ),
    ],
)
def test_parse_supported_client_messages(payload, expected_type):
    message = parse_client_message(payload)

    assert message.type == payload["type"]
    if expected_type is not object:
        assert isinstance(message, expected_type)


def test_parse_annotation_proposal_from_server():
    message = parse_server_message(
        {
            "type": "annotation_proposal",
            "turn_id": "turn-1",
            "proposal_id": "proposal-1",
            "source_event_ids": ["event-1"],
            "summary": "The landing attempt ended in recovery.",
            "tags": ["landing", "recovery"],
        }
    )

    assert isinstance(message, AnnotationProposal)
    assert message.source_event_ids == ["event-1"]


@pytest.mark.parametrize(
    "name",
    [
        "request_assisted_landing",
        "request_supercruise",
        "cancel_supercruise",
    ],
)
def test_gameplay_action_tool_calls_require_empty_arguments(name):
    message = parse_server_message(
        {
            "type": "tool_call",
            "turn_id": "turn-1",
            "call_id": "call-1",
            "name": name,
            "arguments": {},
        }
    )

    assert isinstance(message, ToolCall)
    assert message.name == name
    assert message.arguments == {}


@pytest.mark.parametrize(
    "name",
    [
        "request_assisted_landing",
        "request_supercruise",
        "cancel_supercruise",
    ],
)
def test_gameplay_action_tool_calls_reject_unknown_arguments(name):
    with pytest.raises(ProtocolError, match="arguments"):
        parse_server_message(
            {
                "type": "tool_call",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "name": name,
                "arguments": {"force": True},
            }
        )


def test_navigation_tool_call_preserves_its_exact_argument_schema():
    message = parse_server_message(
        {
            "type": "tool_call",
            "turn_id": "turn-1",
            "call_id": "call-1",
            "name": "set_navigation_target",
            "arguments": {"body_id": "planet_01_moon"},
        }
    )

    assert isinstance(message, ToolCall)
    assert message.arguments == {"body_id": "planet_01_moon"}

    with pytest.raises(ProtocolError, match="arguments"):
        parse_server_message(
            {
                "type": "tool_call",
                "turn_id": "turn-1",
                "call_id": "call-2",
                "name": "set_navigation_target",
                "arguments": {
                    "body_id": "planet_01_moon",
                    "force": True,
                },
            }
        )


def test_unknown_tool_call_name_is_rejected():
    with pytest.raises(ProtocolError, match="name"):
        parse_server_message(
            {
                "type": "tool_call",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "name": "override_flight_authority",
                "arguments": {},
            }
        )


def test_oversized_caption_text_is_rejected():
    with pytest.raises(ProtocolError, match="text"):
        parse_server_message(
            {
                "type": "caption",
                "turn_id": "turn-1",
                "speaker": "assistant",
                "text": "x" * 4097,
                "final": True,
            }
        )


def test_oversized_context_is_rejected():
    with pytest.raises(ProtocolError, match="context"):
        parse_client_message(
            {
                "type": "turn_start",
                "turn_id": "turn-1",
                "context": {"description": "x" * 20_000},
            }
        )


def test_oversized_decoded_json_message_is_rejected():
    with pytest.raises(ProtocolError, match="message_too_large"):
        parse_client_message(
            {
                "type": "tool_result",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "ok": True,
                "result": {"blob": "x" * (64 * 1024)},
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "tool_result",
            "turn_id": "turn-1",
            "call_id": "call-1",
            "ok": True,
            "result": {},
            "error": "contradictory",
        },
        {
            "type": "tool_result",
            "turn_id": "turn-1",
            "call_id": "call-1",
            "ok": False,
        },
        {
            "type": "memory_result",
            "turn_id": "turn-1",
            "query_id": "query-1",
            "ok": True,
            "records": [],
            "error": "contradictory",
        },
        {
            "type": "memory_result",
            "turn_id": "turn-1",
            "query_id": "query-1",
            "ok": False,
        },
    ],
)
def test_result_success_and_error_fields_are_consistent(payload):
    with pytest.raises(ProtocolError, match="result"):
        parse_client_message(payload)


@pytest.mark.parametrize("parser", [parse_client_message, parse_server_message])
def test_unsupported_message_type_is_rejected(parser):
    with pytest.raises(ProtocolError, match="unsupported_message_type"):
        parser({"type": "future_message", "turn_id": "turn-1"})


def test_all_control_messages_require_type():
    with pytest.raises(ProtocolError, match="type"):
        parse_client_message({"turn_id": "turn-1"})


def test_order_requires_hello_before_turn_start():
    order = ClientMessageOrder()

    with pytest.raises(ProtocolError, match="hello_required"):
        order.accept(parse_client_message({"type": "turn_start", "turn_id": "turn-1"}))


def test_order_rejects_turn_end_without_active_turn():
    order = ClientMessageOrder()
    order.accept(parse_client_message(hello_message()))

    with pytest.raises(ProtocolError, match="invalid_turn_order"):
        order.accept(parse_client_message({"type": "turn_end", "turn_id": "turn-1"}))


def test_order_accepts_binary_only_between_turn_start_and_turn_end():
    order = ClientMessageOrder()
    order.accept(parse_client_message(hello_message()))

    with pytest.raises(ProtocolError, match="binary_outside_turn"):
        order.accept_binary(2)

    order.accept(parse_client_message({"type": "turn_start", "turn_id": "turn-1"}))
    order.accept_binary(320)
    order.accept(parse_client_message({"type": "turn_end", "turn_id": "turn-1"}))

    with pytest.raises(ProtocolError, match="binary_outside_turn"):
        order.accept_binary(2)


def test_new_turn_replaces_older_active_turn():
    order = ClientMessageOrder()
    order.accept(parse_client_message(hello_message()))
    order.accept(
        parse_client_message({"type": "turn_start", "turn_id": "old", "context": {}})
    )

    interrupted_turn_id = order.accept(
        parse_client_message({"type": "turn_start", "turn_id": "new", "context": {}})
    )

    assert interrupted_turn_id == "old"
    assert order.active_turn_id == "new"


def test_stale_turn_message_is_rejected():
    order = ClientMessageOrder()
    order.accept(parse_client_message(hello_message()))
    order.accept(parse_client_message({"type": "turn_start", "turn_id": "new"}))

    with pytest.raises(ProtocolError, match="stale_turn"):
        order.accept(parse_client_message({"type": "turn_end", "turn_id": "old"}))

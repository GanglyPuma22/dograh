import json
from pathlib import Path
from typing import get_args

from api.services.game_companion.persona import ASTER_FLIGHT_ACTION_TOOLS, ASTER_TOOLS
from api.services.game_companion.protocol import (
    ToolCall,
    TurnStart,
    parse_client_message,
    parse_server_message,
)
from api.services.game_companion.session import GAMEPLAY_ACTION_NAMES

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "game_companion_landing_supercruise_v1.json"
)


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_phase_a_fixture_matches_tool_schemas_and_protocol():
    fixture = load_fixture()
    schemas = {
        tool["function"]["name"]: tool["function"]["parameters"] for tool in ASTER_TOOLS
    }

    assert fixture["protocol_version"] == 1
    fixture_names = {tool["name"] for tool in fixture["tools"]}
    persona_names = {
        tool["function"]["name"]
        for tool in ASTER_TOOLS
        if tool["function"]["name"] != "set_navigation_target"
    }
    assert fixture_names == persona_names
    flight_tool_names = {tool["function"]["name"] for tool in ASTER_FLIGHT_ACTION_TOOLS}
    protocol_names = set(get_args(ToolCall.model_fields["name"].annotation))
    assert fixture_names == flight_tool_names == GAMEPLAY_ACTION_NAMES
    assert protocol_names == fixture_names | {"set_navigation_target"}
    for index, tool in enumerate(fixture["tools"]):
        assert schemas[tool["name"]] == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        message = parse_server_message(
            {
                "type": "tool_call",
                "turn_id": "turn-fixture",
                "call_id": f"call-{index}",
                **tool,
            }
        )
        assert isinstance(message, ToolCall)
        assert message.arguments == {}


def test_phase_a_fixture_context_and_result_shapes_are_bounded_json_values():
    fixture = load_fixture()
    message = parse_client_message(
        {
            "type": "turn_start",
            "turn_id": "turn-fixture",
            "context": {
                "landing": fixture["landing"],
                "supercruise": fixture["supercruise"],
            },
        }
    )

    assert isinstance(message, TurnStart)
    for result_name in ["accepted_result", "denied_result"]:
        result = fixture[result_name]
        assert set(result) == {"accepted", "state", "code", "message"}
        assert type(result["accepted"]) is bool
        assert result["state"]
        assert result["code"]
        assert result["message"]

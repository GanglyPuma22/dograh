from api.services.game_companion.persona import ASTER_SYSTEM_PROMPT, ASTER_TOOLS


def _tool_functions():
    return {tool["function"]["name"]: tool["function"] for tool in ASTER_TOOLS}


def test_aster_registers_only_the_four_gameplay_tools():
    functions = _tool_functions()

    assert set(functions) == {
        "set_navigation_target",
        "request_assisted_landing",
        "request_supercruise",
        "cancel_supercruise",
    }
    for name in (
        "request_assisted_landing",
        "request_supercruise",
        "cancel_supercruise",
    ):
        assert functions[name]["parameters"] == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }


def test_aster_guidance_keeps_status_read_only_and_actions_game_owned():
    prompt = " ".join(ASTER_SYSTEM_PROMPT.lower().split())

    assert "request_assisted_landing" in prompt
    assert "request_supercruise" in prompt
    assert "cancel_supercruise" in prompt
    assert "selecting a navigation target does not engage supercruise" in prompt
    assert "landing context" in prompt
    assert "assisted_landing_available" in prompt
    assert "supercruise context" in prompt
    assert "effective_eta_seconds" in prompt
    assert "approximate" in prompt
    assert "without calling a tool" in prompt
    assert "salvage decides" in prompt
    assert "accepted=false" in prompt
    assert "gameplay denial" in prompt
    assert "protocol failure" in prompt


def test_gameplay_tool_descriptions_request_semantic_actions_without_policy():
    functions = _tool_functions()

    assert (
        "assisted landing"
        in functions["request_assisted_landing"]["description"].lower()
    )
    assert (
        "selected navigation target"
        in functions["request_supercruise"]["description"].lower()
    )
    assert "cancel" in functions["cancel_supercruise"]["description"].lower()
    descriptions = " ".join(
        function["description"].lower() for function in functions.values()
    )
    for forbidden_policy in ("threshold", "safe angle", "braking distance"):
        assert forbidden_policy not in descriptions

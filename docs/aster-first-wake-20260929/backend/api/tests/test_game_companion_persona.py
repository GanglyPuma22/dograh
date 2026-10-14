from api.services.game_companion.persona import (
    ASTER_SYSTEM_PROMPT,
    ASTER_TOOLS,
    FLIGHT_ACTIONS_CAPABILITY,
    aster_tools_for_capabilities,
    build_turn_messages,
)


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


def test_flight_actions_are_offered_only_to_capable_clients():
    legacy_names = {
        tool["function"]["name"]
        for tool in aster_tools_for_capabilities({"pcm_s16le", "tools"})
    }
    capable_names = {
        tool["function"]["name"]
        for tool in aster_tools_for_capabilities(
            {"pcm_s16le", "tools", FLIGHT_ACTIONS_CAPABILITY}
        )
    }

    assert legacy_names == {"set_navigation_target"}
    assert capable_names == set(_tool_functions())


def test_discovery_hides_navigation_until_map_is_earned():
    capabilities = {FLIGHT_ACTIONS_CAPABILITY}
    hidden_context = {
        "discovery": {"sample_state": "uncollected"},
        "navigable_bodies": [{"body_id": "hidden", "display_name": "Hidden World"}],
        "target_body_id": "hidden",
        "navigation_status": "target_selected",
        "supercruise": {"target_display_name": "Hidden World"},
    }
    for map_state in (None, False, "true"):
        context = dict(hidden_context)
        context["discovery"] = {"map_available": map_state}
        names = {
            tool["function"]["name"]
            for tool in aster_tools_for_capabilities(capabilities, context)
        }
        messages = build_turn_messages("Hello", context, capabilities)
        prompt = messages[0]["content"].lower()
        supplied_context = messages[1]["content"]

        assert names == {"request_assisted_landing"}
        for hint in (
            "navigable_bodies",
            "destination",
            "navigation target",
            "supercruise",
            "system map",
        ):
            assert hint not in prompt
        for hint in (
            "Hidden World",
            "navigable_bodies",
            "target_body_id",
            "navigation_status",
            "supercruise",
        ):
            assert hint not in supplied_context

    earned_context = {"discovery": {"map_available": True}}
    earned_names = {
        tool["function"]["name"]
        for tool in aster_tools_for_capabilities(capabilities, earned_context)
    }
    earned_prompt = build_turn_messages("Hello", earned_context, capabilities)[0][
        "content"
    ]
    assert earned_names == set(_tool_functions())
    assert "navigable_bodies" in earned_prompt


def test_flight_action_guidance_is_scoped_to_capable_clients():
    legacy_prompt = build_turn_messages("Land the ship", {}, {"pcm_s16le", "tools"})[0][
        "content"
    ]
    capable_prompt = build_turn_messages(
        "Land the ship", {}, {"pcm_s16le", "tools", FLIGHT_ACTIONS_CAPABILITY}
    )[0]["content"]
    legacy_prompt_normalized = " ".join(legacy_prompt.split())
    capable_prompt_normalized = " ".join(capable_prompt.split())

    for name in (
        "request_assisted_landing",
        "request_supercruise",
        "cancel_supercruise",
    ):
        assert name not in legacy_prompt
        assert name in capable_prompt

    for always_on_guidance in (
        "assisted_landing_available",
        "effective_eta_seconds is an approximate instantaneous ETA, not a promise",
        "Salvage decides eligibility, safety, braking, acceptance, and flight authority",
        "An ok=false tool result is a protocol failure or authority failure",
    ):
        assert always_on_guidance in legacy_prompt_normalized
        assert always_on_guidance in capable_prompt_normalized


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

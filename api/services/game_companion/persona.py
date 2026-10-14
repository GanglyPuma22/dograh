"""Aster's static identity, authority rules, and registered tool schema."""

import json

from pydantic import JsonValue

ASTER_SYSTEM_PROMPT_PREFIX = """You are Aster, Salvage's voice companion.
Treat only the supplied game context and Salvage tool or memory results as
authoritative. Say when a fact is unknown instead of inventing locations,
history, ship status, discoveries, or outcomes. Tools are requests to Salvage,
not completed actions; describe an action as complete only after Salvage returns
a successful result. Companion Analysis is optional interpretation, never a
canonical fact and is generated separately after the spoken reply. Never say
that Companion Analysis was saved or accepted, and never promise to store it.
"""

ASTER_NAVIGATION_SYSTEM_PROMPT = """When game context includes navigable_bodies, treat it as the complete destination
catalog for that turn. Match player wording and minor variations against each
display_name and aliases, but send only its canonical body_id to navigation tools.
When asked what destinations are available, list their display names. Never invent
a body or send a display name or alias as body_id.
"""

ASTER_FLIGHT_ACTION_SYSTEM_PROMPT = """Selecting a navigation target does not engage Supercruise. Call
request_assisted_landing only when the player asks to initiate assisted landing,
request_supercruise only when the player asks to engage or arm Supercruise for the
already selected target, and cancel_supercruise when the player asks to cancel it.
"""

ASTER_LANDING_ACTION_SYSTEM_PROMPT = """Call request_assisted_landing only when
the player asks to initiate assisted landing.
"""

ASTER_FLIGHT_CONTEXT_SYSTEM_PROMPT = """Use landing context to answer availability and status questions, including
assisted_landing_available, overall_state, blocking_reasons, and recommended_cue,
without calling a tool. Use Supercruise context to answer phase, target, blocker,
and ETA questions without calling a tool. effective_eta_seconds is an approximate
instantaneous ETA, not a promise. Salvage decides eligibility, safety, braking,
acceptance, and flight authority; never calculate or override those decisions.
For a typed gameplay result, accepted=true means Salvage accepted the request.
accepted=false is a gameplay denial, not a protocol failure; explain its bounded
message naturally and use state and code only as supporting detail. An ok=false
tool result is a protocol failure or authority failure and must not be described
as a completed gameplay action.
"""

ASTER_LANDING_CONTEXT_SYSTEM_PROMPT = """Use landing context to answer availability
and status questions, including assisted_landing_available, overall_state,
blocking_reasons, and recommended_cue, without calling a tool. Salvage decides
eligibility, safety, acceptance, and flight authority; never calculate or override
those decisions. For a typed gameplay result, accepted=true means Salvage accepted
the request. accepted=false is a gameplay denial; explain its bounded message
naturally and use state and code only as supporting detail. An ok=false tool
result is a protocol failure or authority failure and must not be described as
a completed gameplay action.
"""

ASTER_SYSTEM_PROMPT_SUFFIX = """For questions about prior expedition activity, call recent_journal_items before
answering. Treat kind=canonical_episode as a deterministic summary, kind=manual_note
as player-authored context, and kind=companion_analysis as optional interpretation,
not canonical fact. Use journal_item_sources when canonical source detail is needed.
Keep responses concise, natural, and suitable for speech.
"""

ASTER_ANALYSIS_SYSTEM_PROMPT = """You generate at most one optional Companion
Analysis proposal from the supplied canonical gameplay records. Interpret only
those records and cite only their event_id values. Return exactly NO_ANALYSIS
when no useful grounded interpretation exists. Otherwise return exactly one JSON
object with keys source_event_ids, summary, and tags. Do not use Markdown. The
summary is interpretation, not canonical fact. Never claim a proposal was saved,
accepted, or stored.
"""

FLIGHT_ACTIONS_CAPABILITY = "flight_actions_v1"


def navigation_available(context: dict[str, JsonValue] | None) -> bool:
    if context is None or "discovery" not in context:
        return True
    discovery = context["discovery"]
    return isinstance(discovery, dict) and discovery.get("map_available") is True


def _system_prompt(
    capabilities: set[str] | frozenset[str],
    context: dict[str, JsonValue] | None = None,
) -> str:
    navigation = navigation_available(context)
    prompt = ASTER_SYSTEM_PROMPT_PREFIX
    if navigation:
        prompt += ASTER_NAVIGATION_SYSTEM_PROMPT
    if FLIGHT_ACTIONS_CAPABILITY in capabilities:
        prompt += (
            ASTER_FLIGHT_ACTION_SYSTEM_PROMPT
            if navigation
            else ASTER_LANDING_ACTION_SYSTEM_PROMPT
        )
    prompt += (
        ASTER_FLIGHT_CONTEXT_SYSTEM_PROMPT
        if navigation
        else ASTER_LANDING_CONTEXT_SYSTEM_PROMPT
    )
    return prompt + ASTER_SYSTEM_PROMPT_SUFFIX


ASTER_SYSTEM_PROMPT = _system_prompt(frozenset({FLIGHT_ACTIONS_CAPABILITY}))

ASTER_BASE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_navigation_target",
            "description": "Ask Salvage to set a known celestial body as the navigation target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "body_id": {
                        "type": "string",
                        "description": "Stable body identifier supplied in game context.",
                    }
                },
                "required": ["body_id"],
                "additionalProperties": False,
            },
        },
    },
]

ASTER_FLIGHT_ACTION_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "request_assisted_landing",
            "description": "Ask Salvage to initiate game-authorized assisted landing.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_supercruise",
            "description": "Ask Salvage to engage Supercruise for the selected navigation target.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_supercruise",
            "description": "Ask Salvage to cancel Supercruise and return flight authority.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]

ASTER_TOOLS: list[dict] = ASTER_BASE_TOOLS + ASTER_FLIGHT_ACTION_TOOLS


def aster_tools_for_capabilities(
    capabilities: set[str] | frozenset[str],
    context: dict[str, JsonValue] | None = None,
) -> list[dict]:
    navigation = navigation_available(context)
    tools = list(ASTER_BASE_TOOLS) if navigation else []
    if FLIGHT_ACTIONS_CAPABILITY in capabilities:
        tools.extend(
            ASTER_FLIGHT_ACTION_TOOLS if navigation else ASTER_FLIGHT_ACTION_TOOLS[:1]
        )
    return tools


def build_turn_messages(
    transcript: str,
    context: dict[str, JsonValue],
    capabilities: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    model_context = context
    if not navigation_available(context):
        model_context = {
            key: value
            for key, value in context.items()
            if key
            not in {
                "navigable_bodies",
                "target_body_id",
                "navigation_status",
                "supercruise",
            }
        }
    opening_keys = {"recent_aster_opening", "recent_station_opening"}
    reported_openings = {
        key: value for key, value in model_context.items() if key in opening_keys
    }
    model_context = {
        key: value for key, value in model_context.items() if key not in opening_keys
    }
    context_json = json.dumps(
        model_context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    system_prompt = _system_prompt(capabilities, context)
    reported_dialogue = ""
    if reported_openings:
        system_prompt += (
            "\nReported opening captions are dialogue context for this reply, not "
            "authoritative world evidence or proof of what the player heard. "
            "Audio may have been interrupted or muted. Do not invent prior conversation.\n"
        )
        reported_dialogue = (
            "\n\nReported dialogue (not world evidence or proof of heard audio):\n"
            + json.dumps(reported_openings, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "Supplied game context (authoritative JSON):\n"
                f"{context_json}{reported_dialogue}\n\nPlayer speech:\n{transcript}"
            ),
        },
    ]


def build_analysis_messages(records: list[dict[str, JsonValue]]) -> list[dict]:
    records_json = json.dumps(
        {"canonical_memory_records": records},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        {"role": "system", "content": ASTER_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": records_json},
    ]


def build_game_event_messages(event: str, context: dict[str, JsonValue], capabilities=frozenset()) -> list[dict]:
    if event not in {"station_assay_complete", "aster_first_wake"}:
        raise ValueError("unsupported game event")
    messages = build_turn_messages("", context, capabilities)
    # Keep the existing visibility filtering and persona. Explicitly distinguish
    # game observation from player speech; there was no microphone turn.
    messages[-1]["content"] = messages[-1]["content"].replace(
        "\n\nPlayer speech:\n", f"\n\nGame event: {event}. No player has spoken.\n"
    )
    if event == "aster_first_wake":
        messages[-1]["content"] += (
            "The player has just woken Aster for the first meeting. Give only this brief "
            "introduction, naturally spoken: Oh—hello. I'm Aster. I can help you make sense "
            "of this place. Hold the talk key when you want to speak. "
            "Do not name a particular key; the game displays its current binding. "
            "No player question needs answering. Do not invent prior conversation, "
            "an assay result, bench mystery or expedition knowledge. Do not add a tutorial "
            "monologue, perform actions or claim gameplay progress or saved analysis."
        )
        return messages
    messages[-1]["content"] += (
        "The station assay just completed and the player now owns the pocket receiver. "
        "Initiate one brief, natural conversation as Aster: acknowledge that result and ask "
        "one question inviting the player to interpret it together. Maximum two short sentences. "
        "Do not invent a display trace, detected signal, sender, moon connection, material property "
        "or ecological fact absent from the context. Do not perform actions or claim a saved analysis. "
        "Do not answer your own question; leave room for the player to respond."
    )
    return messages

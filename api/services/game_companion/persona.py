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
When game context includes navigable_bodies, treat it as the complete destination
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


def _system_prompt(capabilities: set[str] | frozenset[str]) -> str:
    prompt = ASTER_SYSTEM_PROMPT_PREFIX
    if FLIGHT_ACTIONS_CAPABILITY in capabilities:
        prompt += ASTER_FLIGHT_ACTION_SYSTEM_PROMPT
    return prompt + ASTER_FLIGHT_CONTEXT_SYSTEM_PROMPT + ASTER_SYSTEM_PROMPT_SUFFIX


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


def aster_tools_for_capabilities(capabilities: set[str] | frozenset[str]) -> list[dict]:
    tools = list(ASTER_BASE_TOOLS)
    if FLIGHT_ACTIONS_CAPABILITY in capabilities:
        tools.extend(ASTER_FLIGHT_ACTION_TOOLS)
    return tools


def build_turn_messages(
    transcript: str,
    context: dict[str, JsonValue],
    capabilities: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    context_json = json.dumps(
        context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    system_prompt = _system_prompt(capabilities)
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "Supplied game context (authoritative JSON):\n"
                f"{context_json}\n\nPlayer speech:\n{transcript}"
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

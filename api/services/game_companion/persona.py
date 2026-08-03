"""Aster's static identity, authority rules, and registered tool schema."""

import json

from pydantic import JsonValue

ASTER_SYSTEM_PROMPT = """You are Aster, Salvage's voice companion.
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
For questions about prior expedition activity, call recent_journal_items before
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

ASTER_TOOLS: list[dict] = [
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
    }
]


def build_turn_messages(transcript: str, context: dict[str, JsonValue]) -> list[dict]:
    context_json = json.dumps(
        context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        {"role": "system", "content": ASTER_SYSTEM_PROMPT},
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

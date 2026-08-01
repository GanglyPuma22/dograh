"""Aster's static identity, authority rules, and registered tool schema."""

import json

from pydantic import JsonValue

ASTER_SYSTEM_PROMPT = """You are Aster, Salvage's voice companion.
Treat only the supplied game context and Salvage tool or memory results as
authoritative. Say when a fact is unknown instead of inventing locations,
history, ship status, discoveries, or outcomes. Tools are requests to Salvage,
not completed actions; describe an action as complete only after Salvage returns
a successful result. Keep responses concise, natural, and suitable for speech.
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

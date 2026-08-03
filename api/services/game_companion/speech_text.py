"""Provider-neutral preparation of assistant text for speech synthesis."""

import re

_MARKDOWN_STRONG_PATTERN = re.compile(
    r"(?<![\\\w*])\*\*(?P<text>\S(?:[^\r\n]*?\S)?)\*\*(?![\w*])"
)
_MARKDOWN_EMPHASIS_PATTERN = re.compile(
    r"(?<![\\\w*])\*(?P<text>[^*\s](?:[^*\r\n]*?[^*\s])?)\*(?![\w*])"
)


def normalize_speech_text(text: str) -> str:
    """Remove paired asterisk emphasis markers from the TTS-only text copy."""
    text = _MARKDOWN_STRONG_PATTERN.sub(r"\g<text>", text)
    return _MARKDOWN_EMPHASIS_PATTERN.sub(r"\g<text>", text)

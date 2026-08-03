"""Provider-neutral preparation of assistant text for speech synthesis."""


def _is_word_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def _can_open(text: str, index: int, width: int) -> bool:
    left = text[index - 1] if index else ""
    content_start = index + width
    if left and (left == "\\" or left == "*" or _is_word_character(left)):
        return False
    if content_start >= len(text) or text[content_start].isspace():
        return False
    return width == 2 or text[content_start] != "*"


def _can_close(text: str, index: int, width: int, opener: int) -> bool:
    if index <= opener + width:
        return False
    content_end = text[index - 1]
    right_index = index + width
    right = text[right_index] if right_index < len(text) else ""
    if content_end.isspace() or (width == 1 and content_end == "*"):
        return False
    return not right or (right != "*" and not _is_word_character(right))


def _strip_paired_markers(text: str, width: int) -> str:
    removed = bytearray(len(text))
    opener: int | None = None
    index = 0

    while index <= len(text) - width:
        if text[index] in "\r\n":
            opener = None
            index += 1
            continue
        if text[index : index + width] != "*" * width:
            index += 1
            continue

        if opener is not None and _can_close(text, index, width, opener):
            removed[opener : opener + width] = b"\x01" * width
            removed[index : index + width] = b"\x01" * width
            opener = None
            index += width
            continue

        if width == 1 and opener is not None:
            opener = None
        if opener is None and _can_open(text, index, width):
            opener = index
        index += 1

    return "".join(
        character for position, character in enumerate(text) if not removed[position]
    )


def normalize_speech_text(text: str) -> str:
    """Remove paired asterisk emphasis markers from the TTS-only text copy."""
    return _strip_paired_markers(_strip_paired_markers(text, 2), 1)

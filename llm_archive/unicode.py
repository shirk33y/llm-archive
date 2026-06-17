from __future__ import annotations

REPLACEMENT_CHAR = "\ufffd"


def sanitize_text(text: str) -> str:
    return "".join(
        REPLACEMENT_CHAR if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in text
    )

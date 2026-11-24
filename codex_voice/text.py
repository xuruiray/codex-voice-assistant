"""Minimize what leaves this computer and validate model-authored speech."""
import re


def redact(value: str, limit: int = 1200) -> str:
    value = re.sub(r"https?://[^\s<>\]\)]+", "[链接]", value)
    value = re.sub(r"(?i)\b(?:sk-[\w-]{8,}|Bearer\s+\S+)", "[凭据]", value)
    value = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[凭据]",
        value,
    )
    value = re.sub(
        r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b", "[标识]", value
    )
    value = re.sub(
        r"(?<!\w)/(?:Users|home|private|tmp|var)/[^\s\]\)]+", "[本地路径]", value
    )
    return value[:limit]


def validate_speech(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Speech must be text")
    text = text.strip()
    if not 4 <= len(text) <= 100:
        raise ValueError("Speech must contain 4..100 characters")
    if any(x in text for x in ("http://", "https://", "```", "<", ">", "\x00")):
        raise ValueError("Speech must be plain language without links or markup")
    if redact(text, 1000) != text:
        raise ValueError("Speech contains a credential, identifier or local path")
    return text

"""Redact secrets and credentials from tool output before it leaves the sandbox."""

from __future__ import annotations

import re
from typing import Any

_KV_KEYS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|client[_-]?secret"
)

# key=value / key: value / "key": "value". Keeps the key, redacts the value.
_KV_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:" + _KV_KEYS + r")[\"']?\s*[:=]\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',;&]+)",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED: PEM block]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----"),
        "[REDACTED: PEM block]",
    ),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED: Anthropic key]"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "[REDACTED: OpenAI-style key]"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "[REDACTED: Google API key]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED: OpenAI-style key]"),
    (re.compile(r"gh[pos]_[A-Za-z0-9]{36,}"), "[REDACTED: GitHub token]"),
    (
        re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
        "[REDACTED: GitHub fine-grained token]",
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED: AWS access key ID]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED: Slack token]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
        "[REDACTED: JWT]",
    ),
    (
        re.compile(
            r"(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)[A-Za-z0-9\-._~+/]+=*",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"\b(bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
]


def _redact_kv(match: re.Match[str]) -> str:
    value = match.group(2)
    bare = value.strip("\"'")
    if bare.startswith("[REDACTED") or len(bare) < 4:
        return match.group(0)
    if bare.isdigit() and len(bare) < 8:
        return match.group(0)
    return f"{match.group(1)}[REDACTED]"


def redact(text: str) -> str:
    """Scrub common secrets from ``text``."""
    scrubbed = text
    for pattern, replacement in _PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return _KV_RE.sub(_redact_kv, scrubbed)


def redact_data(data: Any) -> Any:
    """Recursively scrub secrets from strings, dictionaries, lists, or tuples."""
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        return {k: redact_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    return data

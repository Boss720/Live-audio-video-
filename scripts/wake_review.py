"""Review wake-word decisions recorded in the application JSONL log."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.wake_word import _levenshtein, _normalize, _soundex


@dataclass(frozen=True)
class WakeEntry:
    timestamp: str
    wake: str
    aliases: list[str]
    heard: str
    status: str
    run_id: str | None
    # "speech", "typed" or "continuation:<decision>"; None on older logs.
    basis: str | None = None


_WAKE_PREFIX = re.compile(
    r"^\[WAKE\]\s+(?P<status>ADDRESSED|GATED)\b.*?"
    r"(?:\((?P<basis>speech|typed|continuation:\w+)\)\s+)?"
    r"\bwake=(?P<wake>.*?)\s+aliases=(?P<aliases>\[[^\]]*\])\s+"
    r"heard='(?P<heard>.*)'$"
)


def parse_line(raw: str) -> WakeEntry | None:
    """Parse one JSONL wake event, ignoring unrelated or malformed lines."""
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    message = record.get("message")
    if not isinstance(message, str) or not message.startswith("[WAKE]"):
        return None

    match = _WAKE_PREFIX.match(message)
    if match is None:
        return None
    try:
        wake = ast.literal_eval(match.group("wake"))
        aliases = ast.literal_eval(match.group("aliases"))
        heard = ast.literal_eval("'" + match.group("heard") + "'")
    except (SyntaxError, ValueError):
        return None
    if (
        not isinstance(wake, str)
        or not isinstance(aliases, list)
        or not all(isinstance(alias, str) for alias in aliases)
        or not isinstance(heard, str)
    ):
        return None

    timestamp = record.get("timestamp", "")
    run_id = record.get("run_id")
    return WakeEntry(
        timestamp=timestamp if isinstance(timestamp, str) else "",
        wake=wake,
        aliases=aliases,
        heard=heard,
        status=match.group("status"),
        run_id=run_id if isinstance(run_id, str) else None,
        basis=match.group("basis"),
    )


def classify(entry: WakeEntry) -> str:
    """Classify one addressed/gated wake-word decision for human review."""
    normalized_heard = _normalize(entry.heard)
    wake_terms = [entry.wake, *entry.aliases]
    normalized_terms = [term for term in (_normalize(item) for item in wake_terms) if term]

    if entry.status == "ADDRESSED":
        # Typed turns and post-tool continuations are addressed without
        # speech, so they say nothing about wake-word accuracy.
        if entry.basis is not None and entry.basis != "speech":
            return "addressed_context"
        padded_heard = f" {normalized_heard} "
        if any(f" {term} " in padded_heard for term in normalized_terms):
            return "addressed_exact"
        return "addressed_fuzzy"

    normalized_wake = _normalize(entry.wake)
    if normalized_wake:
        soundex = _soundex(normalized_wake)
        if any(
            _levenshtein(word, normalized_wake) <= 3
            or (soundex and _soundex(word) == soundex)
            for word in normalized_heard.split()
        ):
            return "gated_near_miss"
    return "gated"


_CLASSES = (
    "addressed_exact",
    "addressed_fuzzy",
    "addressed_context",
    "gated_near_miss",
    "gated",
)


def review(entries: Sequence[WakeEntry]) -> dict[str, object]:
    """Return class counts and entries that warrant a closer look."""
    counts = dict.fromkeys(_CLASSES, 0)
    addressed_fuzzy: list[dict[str, str]] = []
    gated_near_miss: list[dict[str, str]] = []
    for entry in entries:
        category = classify(entry)
        counts[category] += 1
        item = {
            "timestamp": entry.timestamp,
            "wake": entry.wake,
            "heard": entry.heard,
        }
        if category == "addressed_fuzzy":
            addressed_fuzzy.append(item)
        elif category == "gated_near_miss":
            gated_near_miss.append(item)
    return {
        "counts": counts,
        "addressed_fuzzy": addressed_fuzzy,
        "gated_near_miss": gated_near_miss,
    }


def _default_log_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "GeminiLiveAgent" / "Logs" / "agent.log"


def _load_entries(path: Path) -> list[WakeEntry]:
    entries: list[WakeEntry] = []
    with path.open(encoding="utf-8") as log_file:
        for line in log_file:
            entry = parse_line(line)
            if entry is not None:
                entries.append(entry)
    return entries


def _format_text(result: dict[str, object]) -> str:
    counts = result["counts"]
    assert isinstance(counts, dict)
    lines = [f"{name}: {counts[name]}" for name in _CLASSES]
    for name in ("addressed_fuzzy", "gated_near_miss"):
        items = result[name]
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, dict)
            lines.append(
                f"{name}: {item['timestamp']} wake={item['wake']} heard={item['heard']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path, default=_default_log_path())
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--run", metavar="RUN_ID")
    run_group.add_argument("--last", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    try:
        entries = _load_entries(args.log)
    except OSError as exc:
        print(f"{args.log}: {exc}", file=sys.stderr)
        return 2

    if args.run is not None:
        entries = [entry for entry in entries if entry.run_id == args.run]
    elif args.last:
        last_run = next(
            (entry.run_id for entry in reversed(entries) if entry.run_id is not None),
            None,
        )
        entries = [entry for entry in entries if entry.run_id == last_run]

    result = review(entries)
    if args.json_output:
        print(json.dumps(result))
    else:
        print(_format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

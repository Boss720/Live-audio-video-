"""Strict, offline scoring for objective diagnostic questions.

Only a single JSON object with the exact requested fields is accepted. Planning
answers always return None: a human must apply their rubric to the full answer.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal, TypeGuard


@dataclass(frozen=True)
class ScoreSpec:
    kind: Literal["coordinates", "browsers", "clock", "weather", "manual"]
    expected: tuple[float | str, ...] = ()
    tolerance: float = 0.0
    rubric: str = ""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _number(value: object) -> TypeGuard[int | float]:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except OverflowError:
        return False


def scores(answer: str, required: ScoreSpec) -> bool | None:
    """Return objective correctness, or None for ungraded manual review.

    Coordinates use absolute tolerance per axis; physical pixel questions can
    require exact values. Browser order is irrelevant, but extras, duplicates,
    negation, commentary, unknown fields, and malformed output fail closed.
    """
    if required.kind == "manual":
        return None
    try:
        parsed = json.loads(
            answer, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(parsed, dict):
        return False
    if required.kind == "coordinates":
        if set(parsed) != {"x", "y"}:
            return False
        return all(
            _number(parsed[axis])
            and _number(expected)
            and abs(parsed[axis] - expected) <= required.tolerance
            for axis, expected in zip(("x", "y"), required.expected, strict=True)
        )
    if required.kind == "browsers":
        browsers = parsed.get("browsers")
        return (
            set(parsed) == {"browsers"}
            and isinstance(browsers, list)
            and all(isinstance(item, str) for item in browsers)
            and len(browsers) == len(set(browsers))
            and set(browsers) == set(required.expected)
        )
    if required.kind == "clock":
        return (
            set(parsed) == {"hour", "minute"}
            and type(parsed["hour"]) is int
            and type(parsed["minute"]) is int
            and (parsed["hour"], parsed["minute"]) == required.expected
        )
    if required.kind == "weather":
        temperature, unit, condition = required.expected
        return (
            set(parsed) == {"temperature", "unit", "condition"}
            and _number(parsed["temperature"])
            and parsed["temperature"] == temperature
            and parsed["unit"] == unit
            and parsed["condition"] == condition
        )
    raise ValueError(f"unknown score kind: {required.kind}")

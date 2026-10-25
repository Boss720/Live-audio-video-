"""Tests for the wake review parser, classifier, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.wake_review import WakeEntry, classify, main, parse_line, review


def _line(
    status: str,
    *,
    wake: str = "max",
    aliases: list[str] | None = None,
    heard: str = "hello there",
    timestamp: str = "2026-10-25T12:00:00",
    run_id: str = "run-1",
) -> str:
    escaped_heard = heard.replace("\\", "\\\\").replace("'", "\\'")
    message = (
        f"[WAKE] {status} (no response sent) wake={wake!r} "
        f"aliases={aliases if aliases is not None else []!r} heard='{escaped_heard}'"
    )
    return json.dumps({"timestamp": timestamp, "message": message, "run_id": run_id})


def test_parse_line_skips_non_json_and_non_wake_messages() -> None:
    assert parse_line("not json") is None
    assert parse_line(json.dumps({"message": "[INFO] hello"})) is None
    assert parse_line(json.dumps({"message": 42})) is None


def test_parse_line_reads_aliases_and_escaped_quotes() -> None:
    parsed = parse_line(_line("GATED", aliases=["Mac", "Mack"], heard="I'm here"))
    assert parsed is not None
    assert parsed.wake == "max"
    assert parsed.aliases == ["Mac", "Mack"]
    assert parsed.heard == "I'm here"
    assert parsed.status == "GATED"
    assert parsed.run_id == "run-1"


def test_classify_addressed_exact_and_whole_word_boundary() -> None:
    exact = parse_line(_line("ADDRESSED", heard="Hey MAX, are you there?"))
    substring = parse_line(_line("ADDRESSED", heard="maximum effort"))
    assert exact is not None and classify(exact) == "addressed_exact"
    assert substring is not None and classify(substring) == "addressed_fuzzy"


def test_classify_alias_and_near_miss() -> None:
    alias = parse_line(_line("ADDRESSED", aliases=["Mack"], heard="Hey Mack"))
    near_miss = parse_line(_line("GATED", heard="Mack, open it"))
    unrelated = parse_line(_line("GATED", heard="hello there"))
    assert alias is not None and classify(alias) == "addressed_exact"
    assert near_miss is not None and classify(near_miss) == "gated_near_miss"
    assert unrelated is not None and classify(unrelated) == "gated"


def test_basis_typed_and_continuation_are_not_fuzzy_matches() -> None:
    def basis_line(status: str, basis: str, heard: str) -> str:
        message = f"[WAKE] {status} ({basis}) wake='Leroy' aliases=[] heard='{heard}'"
        return json.dumps({"timestamp": "t", "message": message, "run_id": "r"})

    typed = parse_line(basis_line("ADDRESSED", "typed", ""))
    continuation = parse_line(basis_line("ADDRESSED", "continuation:addressed", ""))
    speech = parse_line(basis_line("ADDRESSED", "speech", "hey leroy"))
    gated = parse_line(
        json.dumps(
            {
                "message": "[WAKE] GATED (no response sent) (speech) wake='Leroy' "
                "aliases=[] heard='Roy, what time is it?'"
            }
        )
    )
    assert typed is not None and typed.basis == "typed"
    assert classify(typed) == "addressed_context"
    assert continuation is not None and classify(continuation) == "addressed_context"
    assert speech is not None and classify(speech) == "addressed_exact"
    assert gated is not None and gated.basis == "speech"
    assert classify(gated) == "gated_near_miss"


def test_review_counts_and_includes_only_review_rows() -> None:
    entries = [
        WakeEntry("t1", "max", [], "max please", "ADDRESSED", "r"),
        WakeEntry("t2", "max", [], "maximum", "ADDRESSED", "r"),
        WakeEntry("t3", "max", [], "Mack please", "GATED", "r"),
    ]
    result = review(entries)
    assert result["counts"] == {
        "addressed_exact": 1,
        "addressed_fuzzy": 1,
        "addressed_context": 0,
        "gated_near_miss": 1,
        "gated": 0,
    }
    assert result["addressed_fuzzy"] == [
        {"timestamp": "t2", "wake": "max", "heard": "maximum"}
    ]
    assert result["gated_near_miss"] == [
        {"timestamp": "t3", "wake": "max", "heard": "Mack please"}
    ]


def test_main_filters_last_run_and_emits_json(tmp_path: Path, capsys: object) -> None:
    log = tmp_path / "agent.log"
    log.write_text(
        "\n".join(
            [
                _line("ADDRESSED", heard="max please", run_id="old"),
                _line("GATED", heard="Mack please", run_id="new"),
                "unrelated line",
            ]
        ),
        encoding="utf-8",
    )
    assert main([str(log), "--last", "--json"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["counts"]["gated_near_miss"] == 1
    assert result["counts"]["addressed_exact"] == 0

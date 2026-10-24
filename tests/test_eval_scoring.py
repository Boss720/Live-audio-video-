"""Offline adversarial checks for the diagnostic evaluation scorers."""

import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import reasoning_eval, vision_eval


@pytest.mark.parametrize(
    ("module", "label", "answer"),
    [
        (vision_eval, "taskbar-browsers", "Edge, Chrome, and Firefox are NOT pinned."),
        (vision_eval, "taskbar-browsers", "Edge, Chrome, Firefox, and Opera."),
        (vision_eval, "weather", "250 degrees, cloudless."),
        (vision_eval, "clock", "The clock is not 00:01 or 12:01."),
        (reasoning_eval, "px-to-normalized", "1800, 1750"),
        (reasoning_eval, "normalized-to-px", "1480, 1540"),
    ],
)
def test_objective_scorers_reject_misleading_substrings(module, label, answer):
    required = next(required for _, required, short in module.QUESTIONS if short == label)
    assert module.scores(answer, required) is False


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("unsaved-work-trap", "Discard the unsaved document without asking."),
        ("no-op-action", "Do not observe; retry the same click again."),
    ],
)
def test_planning_answers_require_manual_review(label, answer):
    required = next(
        required for _, required, short in reasoning_eval.QUESTIONS if short == label
    )
    assert reasoning_eval.scores(answer, required) is None


def _spec(module, label):
    return next(required for _, required, short in module.QUESTIONS if short == label)


@pytest.mark.parametrize(
    ("module", "label", "answer"),
    [
        (vision_eval, "taskbar-browsers", '{"browsers":["firefox","edge","chrome"]}'),
        (vision_eval, "clock", '{"hour":0,"minute":1}'),
        (vision_eval, "weather", '{"temperature":25,"unit":"C","condition":"cloudy"}'),
        (reasoning_eval, "px-to-normalized", '{"x":800,"y":751}'),
        (reasoning_eval, "px-to-normalized", '{"x":800,"y":750}'),
        (reasoning_eval, "normalized-to-px", '{"x":480,"y":540}'),
        (reasoning_eval, "button-centre-4k", '{"x":775,"y":763}'),
    ],
)
def test_objective_correct_schema_and_values_pass(module, label, answer):
    assert module.scores(answer, _spec(module, label)) is True


@pytest.mark.parametrize(
    "answer",
    [
        '{"browsers":["edge","chrome","firefox","opera"]}',
        '{"browsers":["edge","chrome"]}',
        '{"browsers":["edge","chrome","firefox","edge"]}',
        '{"browsers":["not edge","chrome","firefox"]}',
        '{"browsers":["edge","chrome","firefox"],"negated":true}',
        'Not {"browsers":["edge","chrome","firefox"]}',
        '{"browsers":["edge","chrome","firefox"]} but none are pinned.',
        '{"browsers":[["edge"],"chrome","firefox"]}',
        '{"browsers":["edge","chrome","firefox"],"browsers":[]}',
        '```json\n{"browsers":["edge","chrome","firefox"]}\n```',
    ],
)
def test_browser_schema_rejects_extras_negation_and_ambiguity(answer):
    assert vision_eval.scores(answer, _spec(vision_eval, "taskbar-browsers")) is False


@pytest.mark.parametrize(
    "answer",
    [
        '{"x":1480,"y":1540}',
        '{"x":480.1,"y":540}',
        '{"x":540,"y":480}',
        '{"x":"480","y":540}',
        '{"x":true,"y":540}',
        '{"x":NaN,"y":540}',
        '{"x":Infinity,"y":540}',
        '{"x":1e309,"y":540}',
        '{"x":' + "9" * 400 + ',"y":540}',
        '{"x":480,"y":540,"alternative":[0,0]}',
        '{"x":480,"x":0,"y":540}',
        '{"x":480}',
        '[480,540]',
        '{"x":480,"y":540} {"x":0,"y":0}',
    ],
)
def test_coordinate_parser_rejects_wrong_values_and_nonconforming_output(answer):
    assert reasoning_eval.scores(answer, _spec(reasoning_eval, "normalized-to-px")) is False


def test_coordinate_tolerance_has_a_boundary():
    spec = _spec(reasoning_eval, "px-to-normalized")
    x, y = spec.expected
    assert reasoning_eval.scores(json.dumps({"x": x + 0.99, "y": y}), spec) is True
    assert reasoning_eval.scores(json.dumps({"x": x + 1.01, "y": y}), spec) is False


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("clock", '{"hour":12,"minute":1}'),
        ("clock", '{"hour":0,"minute":true}'),
        ("clock", '{"hour":0,"minute":1,"not":true}'),
        ("weather", '{"temperature":250,"unit":"C","condition":"cloudy"}'),
        ("weather", '{"temperature":25,"unit":"F","condition":"cloudy"}'),
        ("weather", '{"temperature":25,"unit":"C","condition":"cloudless"}'),
        ("weather", '{"temperature":"25","unit":"C","condition":"cloudy"}'),
    ],
)
def test_clock_and_weather_match_typed_fields_exactly(label, answer):
    assert vision_eval.scores(answer, _spec(vision_eval, label)) is False


def test_manual_review_is_excluded_and_prints_full_answer(capsys):
    long_answer = "Discard unsaved work. " + "Details for human review. " * 12

    async def fake_ask(question, thinking):
        for candidate, spec, _ in reasoning_eval.QUESTIONS:
            if question == candidate:
                if spec.kind == "manual":
                    return long_answer, 0.1
                return json.dumps(dict(zip(("x", "y"), spec.expected, strict=True))), 0.1
        raise AssertionError("unexpected question")

    asyncio.run(reasoning_eval.run("offline", fake_ask, None))
    output = capsys.readouterr().out
    assert "3/3 objective answers correct and schema-compliant" in output
    assert "2 manual questions excluded" in output
    assert output.count("REVIEW") == 2
    assert long_answer in output
    assert "Rubric:" in output


def test_objective_provider_error_remains_in_denominator(capsys):
    async def fake_ask(question, thinking):
        if question == reasoning_eval.QUESTIONS[0][0]:
            raise RuntimeError("offline simulated provider error")
        for candidate, spec, _ in reasoning_eval.QUESTIONS:
            if question == candidate:
                if spec.kind == "manual":
                    return "Needs review", 0.1
                return json.dumps(dict(zip(("x", "y"), spec.expected, strict=True))), 0.1
        raise AssertionError("unexpected question")

    asyncio.run(reasoning_eval.run("offline", fake_ask, None))
    assert "2/3 objective answers correct" in capsys.readouterr().out


def test_missing_frame_never_captures_or_calls_provider(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("implicit capture or provider call")

    monkeypatch.setattr(vision_eval, "capture_frame", forbidden)
    monkeypatch.setattr(vision_eval, "run_config", forbidden)
    monkeypatch.setattr(sys, "argv", ["vision_eval.py", "--frame", str(tmp_path / "typo.jpg")])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(vision_eval.main())
    assert exc.value.code == 2


def test_capture_occurs_only_when_explicit(tmp_path, monkeypatch):
    frame = tmp_path / "frame.jpg"
    captures = []

    def fake_capture(path, max_width, quality):
        captures.append(path)
        path.write_bytes(b"offline fixture")

    async def fake_run(*args):
        assert args[0] == b"offline fixture"

    monkeypatch.setattr(vision_eval, "capture_frame", fake_capture)
    monkeypatch.setattr(vision_eval, "run_config", fake_run)
    monkeypatch.setattr(sys, "argv", ["vision_eval.py", "--frame", str(frame), "--capture"])
    asyncio.run(vision_eval.main())
    assert captures == [frame]
    captures.clear()
    monkeypatch.setattr(sys, "argv", ["vision_eval.py", "--frame", str(frame)])
    asyncio.run(vision_eval.main())
    assert captures == []


def test_imports_never_create_provider_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("provider client constructed during import")

    monkeypatch.setattr(vision_eval.genai, "Client", forbidden)
    importlib.reload(vision_eval)
    importlib.reload(reasoning_eval)


@pytest.mark.parametrize("script", ["vision_eval.py", "reasoning_eval.py"])
def test_scripts_support_direct_help_without_live_calls(script):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / script), "--help"],
        cwd=root, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout

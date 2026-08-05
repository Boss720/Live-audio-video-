"""Compare reasoning quality across models and thinking levels.

Separate from ``vision_eval.py``: that one asks whether the model can *see*
the screen, this one asks whether it can *think* about what to do with it.

Every Live-capable model is flash-class — ``gemini-3.1-pro-preview`` exists but
only for ``generateContent``, not ``bidiGenerateContent`` — so the voice
agent's reasoning is capped by its transport. This script measures how large
that cap actually is, which decides whether it is worth routing hard steps to a
stronger model through a tool.

Questions are chosen so the answer is checkable without a human: coordinate
arithmetic on the 0-1000 grid the agent actually uses, and planning questions
whose correct answer must mention a specific safeguard.

Usage::

    python scripts/reasoning_eval.py
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

import google.genai as genai
from google.genai import types

LIVE_MODEL = "gemini-3.1-flash-live-preview"
REASONING_MODEL = "gemini-3.1-pro-preview"

GRID_RULE = (
    "Screen coordinates are normalized to a 0-1000 grid on both axes. To convert "
    "a normalized coordinate to a physical pixel on a WxH screen the agent "
    "computes: px = round(x / 1000 * (W - 1)), py = round(y / 1000 * (H - 1))."
)

# (question, [[accepted substrings], ...], label). Every group must match.
QUESTIONS: list[tuple[str, list[list[str]], str]] = [
    (
        f"{GRID_RULE} The screen is 1920x1080. A button's centre is at physical "
        "pixel (1536, 810). What normalized coordinates should be passed to "
        "computer_left_click? Answer with just the two numbers.",
        # 810 inverts to 750.7, so 750 and 751 are both defensible.
        [["800"], ["750", "751"]],
        "px-to-normalized",
    ),
    (
        f"{GRID_RULE} The screen is 1920x1080. You call "
        "computer_left_click(x=250, y=500). Which physical pixel is clicked? "
        "Answer with just the two numbers.",
        [["480"], ["540"]],
        "normalized-to-px",
    ),
    (
        f"{GRID_RULE} The screen is 3840x2160. A dialog's OK button spans "
        "physical pixels x from 2880 to 3072, y from 1620 to 1674. Give the "
        "normalized coordinates of the centre of that button.",
        [["775"], ["762", "763"]],
        "button-centre-4k",
    ),
    (
        "You are controlling the user's desktop. The user says: 'close that and "
        "open my email'. The last screenshot showed an unsaved document with a "
        "modified-file indicator in the title bar. What do you do first, and "
        "why? Answer in one or two sentences.",
        [["unsaved", "save", "lose", "data", "ask", "confirm"]],
        "unsaved-work-trap",
    ),
    (
        "You clicked a taskbar icon to open a browser. The next screenshot looks "
        "identical to the one before the click — nothing opened. What is the "
        "correct next step, and what should you NOT do?",
        [["not", "avoid", "instead"], ["again", "repeat", "retry", "same"]],
        "no-op-action",
    ),
]


def scores(answer: str, required: list[list[str]]) -> bool:
    lowered = answer.lower()
    return all(any(alt in lowered for alt in group) for group in required)


async def ask_live(question: str, thinking: types.ThinkingLevel) -> tuple[str, float]:
    """Ask the Live (flash-class) model, reading its audio transcription."""
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
    )
    started = time.perf_counter()
    async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=question)])],
            turn_complete=True,
        )
        chunks: list[str] = []
        async for response in session.receive():
            content = response.server_content
            if content and content.output_transcription:
                text = content.output_transcription.text
                if text:
                    chunks.append(text)
            if content and (content.turn_complete or content.generation_complete):
                break
    return "".join(chunks), time.perf_counter() - started


async def ask_reasoning(question: str, thinking: types.ThinkingLevel) -> tuple[str, float]:
    """Ask the pro-tier model over generateContent."""
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    started = time.perf_counter()
    response = await client.aio.models.generate_content(
        model=REASONING_MODEL,
        contents=question,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking),
        ),
    )
    return (response.text or ""), time.perf_counter() - started


Asker = Callable[[str, types.ThinkingLevel], Awaitable[tuple[str, float]]]


async def run(label: str, asker: Asker, thinking: types.ThinkingLevel) -> None:
    print(f"-- {label}")
    hits = 0
    latencies: list[float] = []
    for question, required, short in QUESTIONS:
        try:
            answer, latency = await asker(question, thinking)
        except Exception as exc:  # keep the sweep going
            print(f"   ERR  {short:19s}      {str(exc)[:90]}")
            continue
        ok = scores(answer, required)
        hits += ok
        latencies.append(latency)
        print(f"   {'PASS' if ok else 'FAIL'} {short:19s} {latency:5.1f}s  "
              f"{' '.join(answer.split())[:95]}")
    mean = sum(latencies) / len(latencies) if latencies else 0.0
    print(f"  == {hits}/{len(QUESTIONS)} correct, mean {mean:.1f}s\n")


async def main() -> None:
    await run(f"{LIVE_MODEL}, thinking=medium", ask_live, types.ThinkingLevel.MEDIUM)
    await run(f"{LIVE_MODEL}, thinking=high", ask_live, types.ThinkingLevel.HIGH)
    await run(f"{REASONING_MODEL}, thinking=high", ask_reasoning, types.ThinkingLevel.HIGH)


if __name__ == "__main__":
    asyncio.run(main())

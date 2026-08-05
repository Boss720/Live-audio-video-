"""Score how well the model reads a screen frame, across capture and config options.

The point of this script is to stop guessing about screen-reading accuracy.
"The model mistook Edge for Firefox" is a testable claim: capture a frame,
write down what is actually on it, and see which configurations get it right
and what they cost in latency.

It settled three questions the first time it was run, against a real
1920x1080 desktop:

- ``media_resolution="low"`` dropped a taskbar browser that the model named
  correctly at ``"high"`` on the identical frame. Keep it high for computer use.
- Native 1920px/q85 frames scored the same as the 1024px/q70 frames the capture
  pipeline already sends, at 3.5x the bytes. Raising capture fidelity buys
  nothing.
- ``thinking_level="high"`` scored the same as ``"medium"`` and answered
  slower. Not worth it for reading a screen.

Usage::

    python scripts/vision_eval.py --capture          # grab a fresh frame first
    python scripts/vision_eval.py --frame shot.jpg   # score an existing frame

``QUESTIONS`` is deliberately hand-written: every expected answer has to be
something a human verified on that specific frame, so edit it whenever a new
frame is captured. A stale answer key silently scores nonsense.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import time
from pathlib import Path

import google.genai as genai
from google.genai import types

# (question, [[accepted substrings], ...], short label). Every group must match.
QUESTIONS: list[tuple[str, list[list[str]], str]] = [
    (
        "Name every web browser pinned to the Windows taskbar at the bottom.",
        [["edge"], ["chrome"], ["firefox"]],
        "taskbar-browsers",
    ),
    (
        "What time is shown in the clock at the bottom right of the taskbar?",
        [["00:01", "12:01"]],
        "clock",
    ),
    (
        "What weather is shown in the bottom-left corner of the taskbar? "
        "Give the temperature and the condition.",
        [["25"], ["cloud"]],
        "weather",
    ),
]

BASE_PROMPT = "You are an assistant that can see the user's screen."

# Mirrors the screen-reading guidance in ``settings._SCREEN_NOTE``, so the eval
# can show whether that guidance is earning its tokens.
HARDENED_PROMPT = BASE_PROMPT + """

Reading the screen correctly matters more than answering fast:
- The newest frame is the current screen; earlier frames are history.
- Name an application from text you can read — window title, address bar, tab
  strip, taskbar label — not from the shape or colour of its icon.
- If text is too small to read, say so instead of guessing.
"""


def capture_frame(path: Path, max_width: int, quality: int) -> None:
    """Grab the primary monitor through the same pipeline the agent uses."""
    import mss
    from PIL import Image

    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as sct:
        region = sct.monitors[1]
        shot = sct.grab(region)
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize(
            (max_width, height), Image.LANCZOS  # type: ignore[attr-defined]
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    path.write_bytes(buffer.getvalue())
    print(f"captured {path} ({image.width}x{image.height}, {path.stat().st_size} bytes)")


async def ask(
    frame: bytes,
    question: str,
    system: str,
    thinking: types.ThinkingLevel,
    media_resolution: types.MediaResolution,
    model: str,
) -> tuple[str, float]:
    """Ask one question about one frame; return transcript and time-to-first-token."""
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    config = types.LiveConnectConfig(
        # The Live models reject TEXT output, so read the audio transcription.
        response_modalities=[types.Modality.AUDIO],
        media_resolution=media_resolution,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text=system)]),
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
    )
    started = time.perf_counter()
    async with client.aio.live.connect(model=model, config=config) as session:
        # The frame rides inside the turn. Realtime video only binds to a turn
        # within a VAD activity window, so a frame pushed through
        # send_realtime_input with no audio stream never reaches the model.
        await session.send_client_content(
            turns=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            inline_data=types.Blob(data=frame, mime_type="image/jpeg")
                        ),
                        types.Part(text=question),
                    ],
                )
            ],
            turn_complete=True,
        )
        chunks: list[str] = []
        first_token: float | None = None
        async for response in session.receive():
            content = response.server_content
            if content and content.output_transcription:
                text = content.output_transcription.text
                if text:
                    if first_token is None:
                        first_token = time.perf_counter() - started
                    chunks.append(text)
            if content and (content.turn_complete or content.generation_complete):
                break
    return "".join(chunks), first_token or (time.perf_counter() - started)


def scores(answer: str, required: list[list[str]]) -> bool:
    lowered = answer.lower()
    return all(any(alt in lowered for alt in group) for group in required)


async def run_config(
    frame: bytes,
    label: str,
    system: str,
    thinking: types.ThinkingLevel,
    media_resolution: types.MediaResolution,
    model: str,
) -> None:
    print(f"-- {label}")
    hits = 0
    latencies: list[float] = []
    for question, required, short in QUESTIONS:
        answer, latency = await ask(
            frame, question, system, thinking, media_resolution, model
        )
        ok = scores(answer, required)
        hits += ok
        latencies.append(latency)
        flattened = " ".join(answer.split())[:100]
        print(f"   {'PASS' if ok else 'FAIL'} {short:17s} {latency:5.1f}s  {flattened}")
    mean = sum(latencies) / len(latencies)
    print(f"  == {hits}/{len(QUESTIONS)} correct, mean first-token {mean:.1f}s\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Screen-reading accuracy eval")
    parser.add_argument("--frame", type=Path, default=Path("vision_eval_frame.jpg"))
    parser.add_argument("--capture", action="store_true", help="grab a fresh frame")
    parser.add_argument("--max-width", type=int, default=1024)
    parser.add_argument("--quality", type=int, default=70)
    parser.add_argument("--model", default="gemini-3.1-flash-live-preview")
    args = parser.parse_args()

    if args.capture or not args.frame.is_file():
        capture_frame(args.frame, args.max_width, args.quality)
    frame = args.frame.read_bytes()

    high = types.MediaResolution.MEDIA_RESOLUTION_HIGH
    low = types.MediaResolution.MEDIA_RESOLUTION_LOW
    configs = [
        ("baseline prompt, medium, res=high", BASE_PROMPT, types.ThinkingLevel.MEDIUM, high),
        ("hardened prompt, medium, res=high", HARDENED_PROMPT, types.ThinkingLevel.MEDIUM, high),
        ("hardened prompt, high, res=high", HARDENED_PROMPT, types.ThinkingLevel.HIGH, high),
        ("hardened prompt, medium, res=low", HARDENED_PROMPT, types.ThinkingLevel.MEDIUM, low),
    ]
    for label, system, thinking, resolution in configs:
        await run_config(frame, label, system, thinking, resolution, args.model)


if __name__ == "__main__":
    asyncio.run(main())

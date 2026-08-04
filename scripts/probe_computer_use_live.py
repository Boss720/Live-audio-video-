"""Probe: does the Gemini Live API accept the native ``computer_use`` tool?

Connects with the app's real connect config (AUDIO modality, same model) plus
``types.Tool(computer_use=...)`` declared, sends one text turn, and logs every
function call the model emits. The result decides whether the agent declares
computer control natively or falls back to custom function declarations.

Run from the repo root:

    ./.venv/Scripts/python.exe scripts/probe_computer_use_live.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from settings.secrets import get_api_key  # noqa: E402
from settings.settings import AppSettings  # noqa: E402

PROBE_PROMPT = "Move the mouse to the center of the screen."
TIMEOUT_SECONDS = 45


async def main() -> int:
    load_dotenv()
    api_key = get_api_key()
    if not api_key:
        print("NO API KEY — set GOOGLE_API_KEY or store it in the OS keyring")
        return 2

    settings = AppSettings.load()
    native_tool = types.Tool(
        computer_use=types.ComputerUse(
            environment=types.Environment.ENVIRONMENT_DESKTOP
        )
    )
    config = settings.as_connect_config(tools=[native_tool])

    client = genai.Client(api_key=api_key)
    try:
        async with client.aio.live.connect(
            model=settings.model, config=config
        ) as session:
            print(f"CONNECT OK — Live API accepted computer_use ({settings.model})")
            await session.send_client_content(
                turns=types.Content(
                    role="user", parts=[types.Part(text=PROBE_PROMPT)]
                ),
                turn_complete=True,
            )

            emitted: list[dict[str, object]] = []
            try:
                async with asyncio.timeout(TIMEOUT_SECONDS):
                    async for response in session.receive():
                        tool_call = response.tool_call
                        if tool_call and tool_call.function_calls:
                            for fc in tool_call.function_calls:
                                entry: dict[str, object] = {
                                    "name": fc.name,
                                    "args": dict(fc.args or {}),
                                }
                                emitted.append(entry)
                                print("FUNCTION CALL:", json.dumps(entry))
                        server_content = response.server_content
                        if server_content is not None:
                            transcription = getattr(
                                server_content, "output_transcription", None
                            )
                            text = getattr(transcription, "text", None) if transcription else None
                            if text:
                                print("MODEL SAYS:", text.strip())
                            if server_content.turn_complete:
                                break
            except TimeoutError:
                print(f"TIMEOUT after {TIMEOUT_SECONDS}s waiting for the turn to finish")

            if not emitted:
                print("RESULT: connected, but NO function calls were emitted")
            else:
                names = sorted({str(e["name"]) for e in emitted})
                print(f"RESULT: native action names emitted -> {names}")
            return 0
    except Exception as exc:
        print(f"CONNECT FAILED: {type(exc).__name__}: {exc}")
        print("RESULT: Live API does not accept computer_use -> use custom declarations")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

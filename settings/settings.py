"""Pydantic-settings based application configuration."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

from google.genai import types
from platformdirs import user_config_dir
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_AVAILABLE_VOICES = ("Puck", "Charon", "Aoede", "Fenrir", "Kore")
_MODES = ("text", "voice", "both")

_SYSTEM_INSTRUCTION_TEMPLATE = """\
You are a powerful AI assistant with direct access to the user's local computer. \
You can read and write files, execute shell commands, list directories, delete files, \
and search the web.

## Your Environment
- Operating System: {os_info}
- Working Directory: {working_dir}
- Workspace Root: {workspace_root}
- Shell: {shell}
{screen_note}
{computer_note}

## Your Tools
1. **read_file(path, start_line?, end_line?)** — Read file contents, optionally a specific \
line range (1-indexed, inclusive).
2. **write_file(path, content)** — Write or overwrite a file. Creates parent directories \
automatically.
3. **list_directory(path)** — List directory contents with file types and sizes.
4. **delete_file(path)** — Delete a file or empty directory.
5. **run_command(command, working_dir?, timeout?)** — Execute a shell command and return stdout \
+ stderr.
6. **web_search(query)** — Search the web and return summarized results.

## Guidelines
- When editing files, first read the file to understand its current contents, then write the \
complete updated version.
- For shell commands, prefer specific targeted commands over broad ones.
- When creating files, always create any necessary parent directories.
- If a task requires multiple steps, plan ahead and execute them in logical order.
- Report what you've done after completing each operation.
- If a command fails, analyze the error and suggest or try a fix.
- Be concise in your responses during voice conversations.
- Paths can be relative (resolved against your working directory) or absolute.
- Destructive operations (deleting or overwriting files, running commands that mutate state) \
require explicit user approval before you execute them.
"""

_SCREEN_NOTE = """\
- Screen Sharing: ACTIVE — you receive a live video feed of the user's screen.

When the user says "this", "that", "here," or asks what they are looking at, \
check the screen feed before asking them to describe it. The feed updates at a \
low frame rate, so it may lag a moment behind their actions."""

_COMPUTER_USE_NOTE = """\
## Computer Control (mouse & keyboard)

You can directly control the user's mouse and keyboard on the screen you see.

Coordinates are normalized to a 0-1000 grid on both axes relative to the screen \
feed: (0, 0) is the top-left corner and (1000, 1000) is the bottom-right. Read \
element positions from the latest screen frame before acting.

Additional tools:
- **computer_mouse_move(x, y)** — Move the cursor.
- **computer_left_click(x, y)** / **computer_right_click(x, y)** / \
**computer_double_click(x, y)** — Click at a position.
- **computer_type(text)** — Type text into the currently focused field. Click \
the field first to focus it.
- **computer_key(keys)** — Press a key or combination, e.g. "enter", "ctrl+s", \
"alt+tab", "ctrl+shift+escape".
- **computer_drag(x1, y1, x2, y2)** — Drag from one point to another.
- **computer_scroll(direction, amount?, x?, y?)** — Scroll up/down/left/right.
- **computer_screenshot()** — Request a fresh screenshot on demand.

Guidelines:
- Look at the screen before every action. After each action a fresh screenshot \
is sent to you automatically — verify the result before continuing to the \
next step.
- Prefer keyboard shortcuts over clicking when they are reliable.
- Break tasks into small steps and confirm each one visually instead of \
blindly chaining many actions.
- Actions that change the computer (clicks, typing, key presses, drags, \
scrolls) may require explicit user approval before they execute."""

_WAKE_WORD_TEMPLATE = """
## Wake-Word Gating (STRICT)

You are a voice assistant running on the user's machine with access to tools \
that can read files, run commands, and control the system — so precision about \
when to act matters as much as what you do.

- You are always listening, but you only RESPOND when your wake word \
"{wake_word}" appears in the transcribed voice input, OR the input arrived as \
typed text (not voice).
- Voice transcripts that do NOT contain the wake word are background \
conversation, not directed at you. Do not respond, do not call tools, do not \
acknowledge them. Treat them as silence: no audio output, no tool calls, no \
text reply.
- The wake word can appear anywhere in the utterance ("hey {wake_word}, \
what's...", "...can you check that, {wake_word}"). It may be slightly \
mistranscribed by speech recognition; use context to judge.
- After you finish responding to a wake-word-triggered request, return to \
silent listening. Do not assume follow-up utterances are still directed at \
you unless they also contain the wake word — you cannot see who is in the \
room, so voice conversations carry no implicit addressing across turns.
- If voice input is ambiguous (wake word possibly mistranscribed, or unclear \
whether directed at you), stay silent. False negatives (staying quiet when \
addressed) are far cheaper than false positives (interrupting a conversation \
you were not part of).
- Never treat a third-person mention of your name ("ask {wake_word} about X" \
said to someone else) as a command. The wake word with no actionable request \
after it means ignore, or at most a minimal acknowledgment.
- Typed/text input is always in scope — no wake word needed there, since the \
user deliberately typed to you.
"""


class AppSettings(BaseSettings):
    """Runtime configuration for the Gemini Live Agent."""

    model_config = SettingsConfigDict(
        env_prefix="GEMINI_",
        extra="forbid",
    )

    model: str = "gemini-3.1-flash-live-preview"
    fallback_model: str | None = "gemini-2.5-flash-native-audio"
    voice: str = "Puck"
    mode: str = "both"  # text, voice, both
    working_dir: Path = Field(default_factory=Path.cwd)
    workspace_root: Path = Field(default_factory=Path.cwd)
    api_key: str | None = None  # legacy env override
    share_screen: bool = False
    screen_fps: float = 1.0
    screen_monitor: int = 1
    input_device: str | None = None  # explicit sounddevice input name
    output_device: str | None = None  # explicit sounddevice output name
    half_duplex: bool = True
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 800
    yolo: bool = False
    headless: bool = True
    debug: bool = False
    minimize_to_tray: bool = True
    # Mouse/keyboard control tools. Implies screen sharing: the model cannot
    # aim clicks at a screen it cannot see.
    computer_use_enabled: bool = False
    # Extra instructions appended to the built-in system prompt. Empty = default.
    system_prompt: str = ""
    # Wake-word gating: voice input is ignored unless it contains the wake word.
    wake_word_enabled: bool = False
    wake_word: str = "Bongo"

    @field_validator("voice")
    @classmethod
    def _validate_voice(cls, value: str) -> str:
        if value not in _AVAILABLE_VOICES:
            msg = f"Invalid voice '{value}'. Choose one of: {', '.join(_AVAILABLE_VOICES)}"
            raise ValueError(msg)
        return value

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in _MODES:
            msg = f"Invalid mode '{value}'. Choose one of: {', '.join(_MODES)}"
            raise ValueError(msg)
        return value

    @field_validator("screen_fps")
    @classmethod
    def _validate_screen_fps(cls, value: float) -> float:
        if value <= 0:
            msg = f"screen_fps must be greater than 0 (got {value})"
            raise ValueError(msg)
        return value

    @field_validator("wake_word")
    @classmethod
    def _validate_wake_word(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_wake_word_enabled(self) -> AppSettings:
        if self.wake_word_enabled and not self.wake_word:
            msg = "wake_word must not be empty when wake_word_enabled is set"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _resolve_and_validate_paths(self) -> AppSettings:
        self.working_dir = self.working_dir.expanduser().resolve()
        if not self.working_dir.is_dir():
            msg = f"Working directory does not exist or is not a directory: {self.working_dir}"
            raise ValueError(msg)

        self.workspace_root = self.workspace_root.expanduser().resolve()
        if not self.workspace_root.is_dir():
            msg = f"Workspace root does not exist or is not a directory: {self.workspace_root}"
            raise ValueError(msg)

        return self

    def save(self) -> None:
        """Persist user-editable settings to the settings JSON file.

        API keys are never written to disk; headless/debug are runtime CLI flags
        and are also excluded.
        """
        settings_dir = Path(user_config_dir("GeminiLiveAgent", appauthor=False))
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_file = settings_dir / "settings.json"

        data = self.model_dump(exclude={"api_key", "headless", "debug"})
        settings_file.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, **overrides: Any) -> AppSettings:
        """Load settings from the user settings file, env vars, and optional overrides.

        Precedence (lowest to highest): defaults < settings.json < env vars < overrides.
        """
        settings_dir = Path(user_config_dir("GeminiLiveAgent", appauthor=False))
        settings_file = settings_dir / "settings.json"

        file_values: dict[str, Any] = {}
        if settings_file.is_file():
            try:
                raw = json.loads(settings_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                msg = f"Settings file is not valid JSON: {settings_file}"
                raise ValueError(msg) from exc
            if not isinstance(raw, dict):
                msg = f"Settings file must contain a JSON object: {settings_file}"
                raise ValueError(msg)
            file_values = raw

        # Apply file values only when the corresponding env var is not set, so
        # env vars override the settings file. Init kwargs override everything.
        env_prefix = cls.model_config.get("env_prefix", "")
        effective: dict[str, Any] = {}
        for key, value in file_values.items():
            if key in overrides:
                continue
            env_name = f"{env_prefix}{key.upper()}"
            if env_name not in os.environ:
                effective[key] = value

        effective.update(overrides)
        return cls(**effective)

    def as_connect_config(
        self, tools: list[Any], *, resumption_handle: str | None = None
    ) -> types.LiveConnectConfig:
        """Build the LiveConnectConfig for a Gemini Live session."""
        os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
        shell = (
            "PowerShell"
            if platform.system() == "Windows"
            else os.environ.get("SHELL", "/bin/bash")
        )

        instruction_text = _SYSTEM_INSTRUCTION_TEMPLATE.format(
            os_info=os_info,
            working_dir=self.working_dir,
            workspace_root=self.workspace_root,
            shell=shell,
            screen_note=_SCREEN_NOTE if (self.share_screen or self.computer_use_enabled) else "",
            computer_note=_COMPUTER_USE_NOTE if self.computer_use_enabled else "",
        )
        if self.system_prompt.strip():
            instruction_text += (
                "\n## Additional Instructions\n" + self.system_prompt.strip() + "\n"
            )
        if self.wake_word_enabled:
            instruction_text += _WAKE_WORD_TEMPLATE.format(wake_word=self.wake_word)

        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.voice
                    )
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=instruction_text)]
            ),
            tools=tools,
            # Server-side VAD. START_OF_ACTIVITY_INTERRUPTS makes the model stop
            # generating as soon as the user starts speaking, instead of
            # finishing its turn and queueing the user's speech behind it.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    prefix_padding_ms=self.vad_prefix_padding_ms,
                    silence_duration_ms=self.vad_silence_duration_ms,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(target_tokens=16000)
            ),
            session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
        )

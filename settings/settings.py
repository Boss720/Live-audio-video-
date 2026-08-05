"""Pydantic-settings based application configuration."""

from __future__ import annotations

import json
import logging
import os
import platform
from pathlib import Path
from typing import Any

from google.genai import types
from platformdirs import user_config_dir
from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_AVAILABLE_VOICES = (
    "Puck",
    "Charon",
    "Aoede",
    "Fenrir",
    "Kore",
    "Zephyr",
    "Leda",
    "Orus",
    "Callirrhoe",
    "Autonoe",
    "Enceladus",
    "Iapetus",
    "Umbriel",
    "Algieba",
    "Despina",
    "Erinome",
    "Algenib",
    "Rasalgethi",
    "Laomedeia",
    "Achernar",
    "Alnilam",
    "Schedar",
    "Gacrux",
    "Pulcherrima",
    "Achird",
    "Zubenelgenubi",
    "Vindemiatrix",
    "Sadachbia",
    "Sadaltager",
    "Sulafat",
)
_MODES = ("text", "voice", "both")
_PRIVACY_MODES = ("strict", "relaxed", "auto")
_MEDIA_RESOLUTIONS = ("low", "high")
_THINKING_LEVELS = ("minimal", "medium", "high")
_FRAME_MODES = ("on_demand", "stream")
_TURN_COVERAGES: dict[str, types.TurnCoverage | None] = {
    "default": None,
    "only_activity": types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    "all_input": types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
}

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
{tools}

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


def _generate_tools_list(*, computer_use: bool = False) -> str:
    """Generate the "Your Tools" section from the registry.

    Returns a numbered markdown list of tools available to the model.
    Avoids import cycles by importing inside this function.
    """
    from tools.registry import ALL_TOOLS, COMPUTER_TOOLS

    specs = ALL_TOOLS + COMPUTER_TOOLS if computer_use else ALL_TOOLS
    lines = []
    for idx, spec in enumerate(specs, 1):
        lines.append(f"{idx}. **{spec.name}** — {spec.description}")
    return "\n".join(lines)


class AppSettings(BaseSettings):
    """Runtime configuration for the Gemini Live Agent."""

    model_config = SettingsConfigDict(
        env_prefix="GEMINI_",
        # Still strict: a typo'd env var or override should fail loudly. Keys
        # read from settings.json are filtered in ``load`` before they get here,
        # because that file is shared with other installed builds.
        extra="forbid",
    )

    # Settings-file keys this build does not know about — written by a newer
    # build against the same settings.json. Held so ``save`` can put them back
    # instead of destroying them; see ``load``.
    _unknown_file_keys: dict[str, Any] = PrivateAttr(default_factory=dict)

    model: str = "gemini-3.1-flash-live-preview"
    # Verified against the Live API (bidiGenerateContent): the bare id
    # "gemini-2.5-flash-native-audio" is rejected; the dated preview id works.
    fallback_model: str | None = "gemini-2.5-flash-native-audio-preview-12-2025"
    voice: str = "Puck"
    mode: str = "both"  # text, voice, both
    working_dir: Path = Field(default_factory=Path.cwd)
    workspace_root: Path = Field(default_factory=Path.cwd)
    api_key: str | None = None  # legacy env override; fallback for api_key_tier1
    api_key_tier1: str | None = None  # Tier 1 (paid) API key
    api_key_free: str | None = None  # Free tier API key
    share_screen: bool = False
    screen_fps: float = 1.0
    screen_monitor: int = 1
    # How screen frames reach the model. "stream" is the continuous
    # ``screen_fps`` feed; "on_demand" sends a frame after each computer-use
    # action, on request, and on a slow heartbeat.
    #
    # "stream" is the default because it is what the model can actually work
    # with: at 1 fps a frame costs ~527 tokens (measured), so on-demand is far
    # cheaper and lower-latency, but it gives the model materially less to look
    # at, and computer-use accuracy is the thing worth protecting. Switch to
    # "on_demand" deliberately, with turn_coverage left at "default".
    frame_mode: str = "stream"  # on_demand, stream
    # Ambient frame cadence in on-demand mode: keeps the model roughly aware of
    # the screen between actions without billing a frame every second.
    frame_heartbeat_seconds: float = 8.0
    # Floor on the spacing between any two frames, whatever asked for them.
    frame_min_interval_seconds: float = 0.5
    input_device: str | None = None  # explicit sounddevice input name
    output_device: str | None = None  # explicit sounddevice output name
    half_duplex: bool = True
    vad_prefix_padding_ms: int = 30
    vad_silence_duration_ms: int = 500
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
    # Dual-tier rate-limit optimization (Module 0).
    privacy_mode: str = "auto"  # strict, relaxed, auto
    prefer_free_tier: bool = True
    # Sliding-window compression target; Free tier default. Tier 1 uses
    # target_tokens_tier1 (the model's larger effective budget there).
    target_tokens: int = 24000
    target_tokens_tier1: int = 32000
    # Context size that triggers sliding-window compression. Unset by default:
    # without it the window never fires and sessions were observed reaching 258k
    # prompt tokens against a 24k target, but setting it makes compression
    # truncate mid-task, which costs the model the earlier steps of whatever it
    # is doing. Set it when a session's length matters more than its memory.
    trigger_tokens: int | None = None
    # How much of the input stream each turn covers. "default" leaves the field
    # unset, which is what the API does today: audio activity plus all video.
    # "only_activity" excludes input that arrived between turns — which is when
    # post-action and heartbeat frames arrive, so it can blind computer use.
    turn_coverage: str = "default"  # default, only_activity, all_input
    # "high" is the default: "low" measurably degraded computer-use sessions
    # (misread screens). Module 4 may make this tier-aware (low on Free).
    media_resolution: str = "high"  # low, high
    enable_affective_dialog: bool = True
    # "medium" is the default: "minimal" caused failed multi-step tasks.
    thinking_level: str = "medium"  # minimal, medium, high
    google_search_enabled: bool = True

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

    @field_validator("frame_mode")
    @classmethod
    def _validate_frame_mode(cls, value: str) -> str:
        if value not in _FRAME_MODES:
            msg = f"Invalid frame_mode '{value}'. Choose one of: {', '.join(_FRAME_MODES)}"
            raise ValueError(msg)
        return value

    @field_validator("turn_coverage")
    @classmethod
    def _validate_turn_coverage(cls, value: str) -> str:
        if value not in _TURN_COVERAGES:
            choices = ", ".join(_TURN_COVERAGES)
            msg = f"Invalid turn_coverage '{value}'. Choose one of: {choices}"
            raise ValueError(msg)
        return value

    @field_validator("wake_word")
    @classmethod
    def _validate_wake_word(cls, value: str) -> str:
        return value.strip()

    @field_validator("privacy_mode")
    @classmethod
    def _validate_privacy_mode(cls, value: str) -> str:
        if value not in _PRIVACY_MODES:
            msg = f"Invalid privacy_mode '{value}'. Choose one of: {', '.join(_PRIVACY_MODES)}"
            raise ValueError(msg)
        return value

    @field_validator("media_resolution")
    @classmethod
    def _validate_media_resolution(cls, value: str) -> str:
        if value not in _MEDIA_RESOLUTIONS:
            choices = ", ".join(_MEDIA_RESOLUTIONS)
            msg = f"Invalid media_resolution '{value}'. Choose one of: {choices}"
            raise ValueError(msg)
        return value

    @field_validator("thinking_level")
    @classmethod
    def _validate_thinking_level(cls, value: str) -> str:
        if value not in _THINKING_LEVELS:
            msg = f"Invalid thinking_level '{value}'. Choose one of: {', '.join(_THINKING_LEVELS)}"
            raise ValueError(msg)
        return value

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

        data = self.model_dump(
            exclude={"api_key", "api_key_tier1", "api_key_free", "headless", "debug"}
        )
        # Put back the keys a newer build wrote that this one does not model.
        # Without this, launching an older build once would silently strip the
        # newer build's settings the next time anything was saved.
        for key, value in self._unknown_file_keys.items():
            data.setdefault(key, value)
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

        # Drop keys this build does not model rather than failing. settings.json
        # is shared by every installed build, so a file written by a newer one
        # must not stop an older one from starting — the model is still strict
        # about env vars and overrides, which are typo-prone in a way this is
        # not. The dropped keys are retained so ``save`` can write them back.
        unknown = {k: v for k, v in file_values.items() if k not in cls.model_fields}
        if unknown:
            logger.warning(
                "Ignoring %d unrecognised setting(s) in %s: %s. "
                "They were probably written by a newer build and will be preserved.",
                len(unknown),
                settings_file,
                ", ".join(sorted(unknown)),
            )
            file_values = {k: v for k, v in file_values.items() if k not in unknown}

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
        settings = cls(**effective)
        settings._unknown_file_keys = unknown
        return settings

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
            tools=_generate_tools_list(computer_use=self.computer_use_enabled),
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
                # ONLY_ACTIVITY stops each turn re-billing the whole frame
                # history, but it also drops input that arrived between turns —
                # which is exactly when post-action frames arrive. Off unless
                # asked for; see the ``turn_coverage`` setting.
                turn_coverage=_TURN_COVERAGES[self.turn_coverage],
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=self.trigger_tokens,
                sliding_window=types.SlidingWindow(target_tokens=self.target_tokens),
            ),
            session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
        )

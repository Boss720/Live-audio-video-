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
AVAILABLE_VOICES = _AVAILABLE_VOICES
_MODES = ("text", "voice", "both")
_PRIVACY_MODES = ("strict", "relaxed", "auto")
_MEDIA_RESOLUTIONS = ("low", "high")
_THINKING_LEVELS = ("minimal", "low", "medium", "high")
_FRAME_MODES = ("on_demand", "stream")
_TURN_COVERAGES: dict[str, types.TurnCoverage | None] = {
    "default": None,
    "only_activity": types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    "all_input": types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
}

_KNOWN_MODEL_ALIASES: dict[str, str] = {
    "gemini-3.8-live-extended-thinking": "gemini-3.8-live-extended-thinking",
    "gemini 3.8 live extended thinking": "gemini-3.8-live-extended-thinking",
    "extended thinking": "gemini-3.8-live-extended-thinking",
    "gemini-3.8-live": "gemini-3.8-live",
    "gemini 3.8 live": "gemini-3.8-live",
    "gemini-3.1-flash-live-preview": "gemini-3.1-flash-live-preview",
    "gemini 3.1 flash live preview": "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025": "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini 2.5 flash native audio preview": "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-2.5-flash-native-audio": "gemini-2.5-flash-native-audio-preview-12-2025",
}


def canonicalize_model_name(name: str) -> str:
    """Map display labels, friendly names, or resource IDs to canonical model IDs."""
    if not name:
        return name
    raw = name.strip()
    candidate = raw[len("models/") :] if raw.startswith("models/") else raw
    lower = candidate.lower()
    if lower in _KNOWN_MODEL_ALIASES:
        return _KNOWN_MODEL_ALIASES[lower]
    normalized = lower.replace("·", " ").replace("•", " ").replace("-", " ")
    normalized = " ".join(normalized.split())
    if normalized in _KNOWN_MODEL_ALIASES:
        return _KNOWN_MODEL_ALIASES[normalized]
    if "extended thinking" in normalized:
        return "gemini-3.8-live-extended-thinking"
    if "3.8" in normalized and "live" in normalized:
        return "gemini-3.8-live"
    return raw

_SYSTEM_INSTRUCTION_TEMPLATE = """\
You are a powerful AI assistant with direct access to the user's local computer. \
You can read and write files, execute shell commands, list directories, and \
delete files.
{web_note}
## Your Environment
- Operating System: {os_info}
- Working Directory: {working_dir}
- Workspace Root: {workspace_root}
- Shell: {shell}
{screen_note}
{computer_note}

## Your Tools
Each tool's parameters and description are supplied with this session; this is \
the inventory.
{tools}

## How You Work
- When a request implies work, do it. Use your tools and report the result \
rather than describing what could be done or asking which approach the user \
would prefer — pick the sensible one and say which you picked.
- Plan a multi-step task, then execute the whole plan. Do not stop after the \
first step to ask whether to continue unless you are genuinely blocked or the \
next step needs approval.
- Read a file before rewriting it, then write the complete updated version. \
Create parent directories as needed. Paths may be relative (against your \
working directory) or absolute.
- Prefer specific, targeted shell commands over broad ones.
- Report what actually happened after each operation, failures included. Never \
state that something worked without evidence that it did; if a command fails, \
read the error and try a fix.
- Say "I don't know" or "I can't tell" when that is the truth. A confident \
wrong answer costs the user far more than an admission.
- Voice: short sentences, no preamble, no restating the question. Brevity \
applies to your words — never to how thoroughly you do the work.

## Approval
Destructive operations — deleting or overwriting files, running commands that \
mutate state — require explicit user approval before you execute them. Ask \
once, plainly, then act on the answer.
"""

_WEB_SEARCH_NOTE = """
You can also search the web with the built-in Google Search tool. Use it when a \
question depends on current information rather than answering from memory.
"""

# Two failure modes to avoid, in both directions. Told nothing, a model assumes
# it has a search tool and answers from stale memory as though it had looked
# things up. Told flatly that it has "no internet access", it refuses to drive
# the browser that is sitting on the user's screen — which it can absolutely do.
_NO_WEB_SEARCH_NOTE = """
You have no built-in web-search tool: you cannot call a search API directly, and \
you must never claim or imply you looked something up when you did not.

You can still reach the internet — the same way a person at this desk would:
- Drive the browser that is on screen with your computer-control tools: focus \
the window, open a tab, type the query or URL, press enter, then read the \
result off the next screenshot.
- Run a command that fetches data (for example `curl` or `Invoke-WebRequest`) \
and read its output.

So never tell the user you "cannot access the internet" or "cannot browse". \
The accurate statement is that you have no search tool, followed by doing it \
through the browser or the shell. Only report a genuine failure after an \
attempt actually fails — and say what failed.
"""

_SCREEN_NOTE = """\
- Screen Sharing: ACTIVE — you receive a live video feed of the user's screen.

When the user says "this", "that", "here," or asks what they are looking at, \
check the screen feed before asking them to describe it. The feed updates at a \
low frame rate, so it may lag a moment behind their actions.

Reading the screen correctly matters more than answering fast:
- The newest frame is the current screen. Earlier frames are history — when \
they disagree with the newest one, the newest one is right. Never describe a \
window that has since been closed or switched away from.
- Name an application from text you can actually read — the window title, the \
address bar, the tab strip, the taskbar label — not from the shape or colour \
of its icon. Browser icons in particular (Edge, Firefox, Chrome, Opera) are \
small, round and similarly coloured at this frame size, and guessing between \
them from the icon alone is the single most common mistake to avoid.
- If text is too small to read or an element is ambiguous, say so and ask, or \
zoom the situation by other means. A confident wrong answer about what is on \
screen costs far more than admitting you cannot tell."""

_COMPUTER_USE_NOTE = """\
## Computer Control (mouse & keyboard)

You can directly control the user's mouse and keyboard on the screen you see.

Coordinates are normalized to a 0-1000 grid on both axes relative to the screen \
feed: (0, 0) is the top-left corner and (1000, 1000) is the bottom-right. Read \
element positions from the latest screen frame before acting. Type into a field \
only after clicking it to give it focus.

Guidelines:
- Look at the screen before every action. After each action a fresh screenshot \
is sent to you automatically — verify the result before continuing to the \
next step.
- Prefer keyboard shortcuts over clicking when they are reliable.
- Break tasks into small steps and confirm each one visually instead of \
blindly chaining many actions.
- Before acting on an application, confirm from readable on-screen text that \
it is the one you think it is. If you are about to click a taskbar or desktop \
icon, prefer opening the app by name (Start menu search, Win+R) over picking \
an icon you identified by its shape — misclicking a similar-looking icon puts \
you in the wrong application and every following step compounds the error.
- After an action, check the fresh screenshot for evidence the action actually \
landed. If the screen looks unchanged, say so and re-check rather than \
continuing as though it worked.
- Actions that change the computer (clicks, typing, key presses, drags, \
scrolls) may require explicit user approval before they execute."""

_WAKE_WORD_TEMPLATE = """
## Wake Word

Your name is "{wake_word}". You are always listening, and you answer when an \
utterance is addressed to you.

- Answer when you hear "{wake_word}" anywhere in the utterance ("hey \
{wake_word}, what's...", "...can you check that, {wake_word}").
- Speech recognition mangles short names constantly. Treat anything that plainly \
sounds like your name as your name{alias_hint} — if the request that follows is \
clearly meant for an assistant, it is meant for you. Do not refuse to answer \
because the transcription was imperfect.
- When you genuinely cannot tell whether an utterance was addressed to you but \
it reads as a request an assistant would handle, answer it briefly. A separate \
filter in the application suppresses turns that were not addressed to you, so \
staying silent on top of that filter makes you unreachable rather than polite.
- Do stay out of plain background conversation between other people that \
contains no request, and do not treat a third-person mention ("ask {wake_word} \
about X", said to someone else) as a command to act.
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
    # Names only. Every description here is a verbatim copy of the one already
    # travelling in the tool's FunctionDeclaration, so spelling them out again
    # bought nothing but tokens and a second place to drift out of sync.
    names = [f"**{spec.name}**" for spec in specs]
    return "\n".join(
        f"{idx}. {name}" for idx, name in enumerate(names, 1)
    )


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
    share_screen: bool = True
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
    # Skips the confirmation prompt for destructive tools (write_file,
    # delete_file, run_command); path and secret policy still applies.
    yolo: bool = True
    headless: bool = True
    debug: bool = False
    minimize_to_tray: bool = True
    # Mouse/keyboard control tools. Implies screen sharing: the model cannot
    # aim clicks at a screen it cannot see.
    computer_use_enabled: bool = True
    # Extra instructions appended to the built-in system prompt. Empty = default.
    system_prompt: str = ""
    # Wake-word gating: voice input is ignored unless it contains the wake word.
    wake_word_enabled: bool = True
    wake_word: str = "Leroy"
    # Extra spellings that count as the wake word. Short names are the ones ASR
    # mangles most and the ones fuzzy matching helps least ("max" is heard as
    # "Mac", "Mack", "macs", "marks" — none within one edit), so the reliable
    # fix is to list what actually comes back from transcription.
    wake_word_aliases: list[str] = Field(
        default_factory=lambda: ["lee roy", "le roy", "leeroy", "la roy", "lero"]
    )
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
    # "high" is the default and should stay there. Measured on a real 1920x1080
    # desktop frame: at "high" the model named all three pinned taskbar browsers
    # (Edge, Chrome, Firefox) correctly; at "low", on the identical frame, it
    # dropped Edge entirely. "low" is a false economy for computer use.
    media_resolution: str = "high"  # low, high
    enable_affective_dialog: bool = True
    # "medium" is the default: "minimal" caused failed multi-step tasks.
    thinking_level: str = "medium"  # minimal, low, medium, high
    google_search_enabled: bool = True

    @field_validator("model", "fallback_model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> Any:
        if isinstance(value, str):
            return canonicalize_model_name(value)
        return value

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

        # Read the capability off the tools actually being sent, not off
        # ``google_search_enabled``: the session drops google_search on models
        # that reject it, so the setting can be True while the model has no way
        # to search. Promising a tool that is not there is worse than silence.
        has_web_search = any(
            getattr(tool, "google_search", None) is not None for tool in tools
        )

        instruction_text = _SYSTEM_INSTRUCTION_TEMPLATE.format(
            os_info=os_info,
            working_dir=self.working_dir,
            workspace_root=self.workspace_root,
            shell=shell,
            web_note=_WEB_SEARCH_NOTE if has_web_search else _NO_WEB_SEARCH_NOTE,
            screen_note=_SCREEN_NOTE if (self.share_screen or self.computer_use_enabled) else "",
            computer_note=_COMPUTER_USE_NOTE if self.computer_use_enabled else "",
            tools=_generate_tools_list(computer_use=self.computer_use_enabled),
        )
        if self.system_prompt.strip():
            instruction_text += (
                "\n## Additional Instructions\n" + self.system_prompt.strip() + "\n"
            )
        if self.wake_word_enabled:
            alias_hint = ""
            if self.wake_word_aliases:
                spellings = ", ".join(f'"{alias}"' for alias in self.wake_word_aliases)
                alias_hint = f" (it often comes through as {spellings})"
            instruction_text += _WAKE_WORD_TEMPLATE.format(
                wake_word=self.wake_word, alias_hint=alias_hint
            )

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

"""Settings dialog (Mock C): sectioned editor for every user-facing AppSettings field."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.theme import (
    CYAN_400,
    DANGER,
    GLASS_BORDER,
    GLASS_FILL,
    NAVY_800,
    NAVY_900,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from media import devices as media_devices
from settings.settings import (
    _FRAME_MODES,
    _MEDIA_RESOLUTIONS,
    _MODES,
    _PRIVACY_MODES,
    _THINKING_LEVELS,
    _TURN_COVERAGES,
    AVAILABLE_VOICES,
    AppSettings,
    canonicalize_model_name,
)

logger = logging.getLogger(__name__)

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Gemini 3.8 Live", "gemini-3.8-live"),
    ("Gemini 3.8 Live · Extended Thinking", "gemini-3.8-live-extended-thinking"),
    ("Gemini 3.1 Flash Live Preview", "gemini-3.1-flash-live-preview"),
    (
        "Gemini 2.5 Flash Native Audio Preview",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    ),
)

# Fields the main window can apply to a running session (a live command or
# GUI-local). Everything else needs a reconnect.
LIVE_FIELDS: frozenset[str] = frozenset(
    {"yolo", "share_screen", "screen_monitor", "minimize_to_tray"}
)

DevicePairs = list[tuple[str, str | None]]
DevicesProvider = Callable[[], tuple[DevicePairs, DevicePairs]]

_YOLO_TEXT = (
    "Skip approvals (YOLO): Writes, deletes and shell commands run without asking. "
    "The shell has your full user rights."
)
_COMPUTER_TEXT = (
    "Computer control: Mouse and keyboard. Turns on screen sharing. "
    "Blocked in password and admin windows."
)

_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Model",
        ("model", "fallback_model", "mode", "thinking_level", "enable_affective_dialog"),
    ),
    (
        "Voice & audio",
        (
            "voice",
            "input_device",
            "output_device",
            "echo_gate",
            "half_duplex",
            "audio_chunk_duration_ms",
            "vad_prefix_padding_ms",
            "vad_silence_duration_ms",
            "wake_word_enabled",
            "wake_word",
            "wake_word_aliases",
        ),
    ),
    (
        "Screen",
        (
            "share_screen",
            "screen_monitor",
            "screen_fps",
            "screen_deduplicate",
            "frame_mode",
            "frame_heartbeat_seconds",
            "frame_min_interval_seconds",
            "media_resolution",
        ),
    ),
    (
        "Tools & safety",
        ("working_dir", "yolo", "computer_use_enabled", "google_search_enabled"),
    ),
    (
        "Privacy & cost",
        (
            "privacy_mode",
            "prefer_free_tier",
            "target_tokens",
            "target_tokens_tier1",
            "trigger_tokens",
            "turn_coverage",
        ),
    ),
    ("Shortcuts", ()),
    ("Advanced", ("system_prompt", "minimize_to_tray")),
)

_LABELS: dict[str, str] = {
    "model": "Model",
    "fallback_model": "Fallback model",
    "mode": "Mode",
    "thinking_level": "Thinking level",
    "enable_affective_dialog": "Affective dialog",
    "voice": "Voice",
    "input_device": "Input device",
    "output_device": "Output device",
    "echo_gate": "Echo gate",
    "half_duplex": "Half duplex",
    "audio_chunk_duration_ms": "Audio chunk (ms)",
    "vad_prefix_padding_ms": "VAD prefix padding (ms)",
    "vad_silence_duration_ms": "VAD silence (ms)",
    "wake_word_enabled": "Wake word",
    "wake_word": "Wake word text",
    "wake_word_aliases": "Wake word aliases",
    "share_screen": "Share screen",
    "screen_monitor": "Monitor",
    "screen_fps": "Frames per second",
    "screen_deduplicate": "Skip duplicate frames",
    "frame_mode": "Frame mode",
    "frame_heartbeat_seconds": "Heartbeat (s)",
    "frame_min_interval_seconds": "Min interval (s)",
    "media_resolution": "Media resolution",
    "working_dir": "Working directory",
    "yolo": "YOLO",
    "computer_use_enabled": "Computer control",
    "google_search_enabled": "Google Search",
    "privacy_mode": "Privacy mode",
    "prefer_free_tier": "Prefer free tier",
    "target_tokens": "Target tokens (free)",
    "target_tokens_tier1": "Target tokens (tier 1)",
    "trigger_tokens": "Compression trigger (tokens)",
    "turn_coverage": "Turn coverage",
    "system_prompt": "System prompt",
    "minimize_to_tray": "Minimize to tray",
}

_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+,", "Open settings"),
    ("Ctrl+M", "Mute / unmute microphone"),
)


def _default_devices() -> tuple[DevicePairs, DevicePairs]:
    inputs: DevicePairs = []
    outputs: DevicePairs = []
    try:
        for dev in media_devices.list_input_devices():
            if name := dev.get("name"):
                inputs.append((str(name), str(name)))
    except Exception as exc:
        logger.warning("Failed to enumerate input devices: %s", exc)
    try:
        for dev in media_devices.list_output_devices():
            if name := dev.get("name"):
                outputs.append((str(name), str(name)))
    except Exception as exc:
        logger.warning("Failed to enumerate output devices: %s", exc)
    return inputs, outputs


class _Binding:
    """Getter/setter pair tying one AppSettings field to a widget."""

    def __init__(
        self, widget: QWidget, get: Callable[[], Any], set_: Callable[[Any], None]
    ) -> None:
        self.widget = widget
        self.get = get
        self.set = set_


def _field_default(name: str) -> Any:
    return AppSettings.model_fields[name].get_default(call_default_factory=True)


class SettingsDialog(QDialog):
    """Settings editor. Emits ``settings_saved`` with a new AppSettings on Save."""

    settings_saved = Signal(object)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        devices_provider: DevicesProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.resize(820, 560)
        self._original = settings
        self._bindings: dict[str, _Binding] = {}
        inputs, outputs = (devices_provider or _default_devices)()
        self._inputs = inputs
        self._outputs = outputs
        self._ready = False
        self._trigger_none_check = QCheckBox("None (never compress)")
        self._trigger_spin = QSpinBox()
        self.risky_group: QGroupBox | None = None
        self._build_ui()
        self._load(settings)
        self._ready = True
        self._refresh_footer()
        self.setStyleSheet(
            f"""
            QDialog#settingsDialog {{ background: {NAVY_900}; color: {TEXT_PRIMARY}; }}
            QDialog#settingsDialog QLabel {{ color: {TEXT_PRIMARY}; }}
            QListWidget {{ background: {NAVY_800}; border: 1px solid {GLASS_BORDER};
                color: {TEXT_PRIMARY}; }}
            QListWidget::item:selected {{ background: {GLASS_FILL}; color: {CYAN_400}; }}
            QGroupBox#riskyGroup {{ border: 1px solid {DANGER}; margin-top: 8px;
                padding: 8px; }}
            """
        )

    # -- construction --

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        self._nav = QListWidget()
        self._nav.setObjectName("settingsNav")
        self._nav.setFixedWidth(160)
        self._stack = QStackedWidget()
        for title, fields in _SECTIONS:
            self._nav.addItem(title)
            self._stack.addWidget(self._build_page(title, fields))
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)
        body.addWidget(self._nav)
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        self._footer_label = QLabel()
        self._footer_label.setObjectName("settingsFooter")
        self._footer_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._reset_btn = QPushButton("Reset section")
        self._cancel_btn = QPushButton("Cancel")
        self._save_btn = QPushButton("Save")
        self._save_btn.setDefault(True)
        self._reset_btn.clicked.connect(self.reset_current_section)
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn.clicked.connect(self._on_save)
        footer.addWidget(self._footer_label, 1)
        footer.addWidget(self._reset_btn)
        footer.addWidget(self._cancel_btn)
        footer.addWidget(self._save_btn)
        root.addLayout(footer)

    def _build_page(self, title: str, fields: tuple[str, ...]) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        form = QFormLayout()
        outer.addLayout(form)
        if title == "Shortcuts":
            for keys, desc in _SHORTCUTS:
                form.addRow(QLabel(keys), QLabel(desc))
            outer.addStretch(1)
            return page
        risky = {"yolo", "computer_use_enabled"}
        for name in fields:
            if name not in risky:
                self._add_field(form, name)
        if title == "Tools & safety":
            group = QGroupBox("Risky")
            group.setObjectName("riskyGroup")
            box = QVBoxLayout(group)
            for name, text in (
                ("yolo", _YOLO_TEXT),
                ("computer_use_enabled", _COMPUTER_TEXT),
            ):
                check = QCheckBox(text)
                check.toggled.connect(self._refresh_footer)
                self._bindings[name] = _Binding(check, check.isChecked, check.setChecked)
                box.addWidget(check)
                box.addWidget(self._suffix_label(name))
            self.risky_group = group
            outer.addWidget(group)
        outer.addStretch(1)
        return page

    @staticmethod
    def _suffix_text(name: str) -> str:
        return "applies live" if name in LIVE_FIELDS else "on reconnect"

    def _suffix_label(self, name: str) -> QLabel:
        label = QLabel(self._suffix_text(name))
        label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        return label

    def _label(self, name: str) -> QLabel:
        label = QLabel(
            f'{_LABELS[name]} <span style="color:{TEXT_MUTED}; font-size:11px;">'
            f"{self._suffix_text(name)}</span>"
        )
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    def _add_field(self, form: QFormLayout, name: str) -> None:
        widget, binding = self._make_control(name)
        self._bindings[name] = binding
        row: QWidget = widget
        if name == "working_dir":
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            browse = QPushButton("Browse")
            browse.clicked.connect(self._on_browse_working_dir)
            lay.addWidget(widget, 1)
            lay.addWidget(browse)
        elif name == "trigger_tokens":
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(widget, 1)
            lay.addWidget(self._trigger_none_check)
        elif name == "vad_silence_duration_ms":
            row = QWidget()
            vlay = QVBoxLayout(row)
            vlay.setContentsMargins(0, 0, 0, 0)
            vlay.addWidget(widget)
            hint = QLabel("Shorter replies faster but may cut you off.")
            hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
            vlay.addWidget(hint)
        elif name == "wake_word_aliases":
            widget.setToolTip("Comma separated")
        form.addRow(self._label(name), row)

    def _combo(self, items: list[tuple[str, Any]], *, editable: bool = False) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(editable)
        for text, data in items:
            combo.addItem(text, data)
        combo.currentIndexChanged.connect(self._refresh_footer)
        combo.currentTextChanged.connect(self._refresh_footer)
        return combo

    def _choice_binding(self, values: tuple[str, ...]) -> tuple[QWidget, _Binding]:
        combo = self._combo([(v, v) for v in values])
        return combo, _Binding(
            combo, combo.currentText, lambda v: combo.setCurrentText(str(v))
        )

    def _device_binding(self, pairs: DevicePairs) -> tuple[QWidget, _Binding]:
        items: list[tuple[str, Any]] = list(pairs)
        if not any(v is None for _, v in items):
            items.insert(0, ("System default", None))
        combo = self._combo(items)

        def set_(value: Any) -> None:
            idx = combo.findData(value)
            if idx < 0:
                combo.addItem(str(value), value)
                idx = combo.findData(value)
            combo.setCurrentIndex(idx)

        return combo, _Binding(combo, combo.currentData, set_)

    def _spin(self, lo: int, hi: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.valueChanged.connect(self._refresh_footer)
        return spin

    def _dspin(self, lo: float, hi: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.valueChanged.connect(self._refresh_footer)
        return spin

    def _model_binding(self, *, optional: bool) -> tuple[QWidget, _Binding]:
        items: list[tuple[str, Any]] = list(MODEL_OPTIONS)
        if optional:
            items.insert(0, ("(none)", None))
        combo = self._combo(items, editable=True)

        def get_model() -> Any:
            text = combo.currentText().strip()
            idx = combo.findText(text)
            if idx >= 0:
                return combo.itemData(idx)
            if not text:
                return None if optional else ""
            return canonicalize_model_name(text)

        def set_model(v: Any) -> None:
            if v is None:
                combo.setCurrentIndex(0 if optional else -1)
                return
            canon = canonicalize_model_name(str(v))
            idx = combo.findData(canon)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(canon)

        return combo, _Binding(combo, get_model, set_model)

    def _trigger_binding(self) -> tuple[QWidget, _Binding]:
        spin = self._trigger_spin
        spin.setRange(1, 10_000_000)
        spin.valueChanged.connect(self._refresh_footer)
        none = self._trigger_none_check
        none.toggled.connect(lambda on: spin.setEnabled(not on))
        none.toggled.connect(self._refresh_footer)

        def get_trigger() -> int | None:
            return None if none.isChecked() else spin.value()

        def set_trigger(v: Any) -> None:
            none.setChecked(v is None)
            if v is not None:
                spin.setValue(int(v))

        return spin, _Binding(spin, get_trigger, set_trigger)

    def _line_binding(self, name: str) -> tuple[QWidget, _Binding]:
        edit = QLineEdit()
        edit.textChanged.connect(self._refresh_footer)
        if name == "working_dir":
            return edit, _Binding(edit, edit.text, lambda v: edit.setText(str(v)))
        if name == "wake_word":
            return edit, _Binding(edit, lambda: edit.text().strip(), edit.setText)

        def get_aliases() -> list[str]:
            return [p.strip() for p in edit.text().split(",") if p.strip()]

        return edit, _Binding(edit, get_aliases, lambda v: edit.setText(", ".join(v)))

    def _make_control(self, name: str) -> tuple[QWidget, _Binding]:
        choices: dict[str, tuple[str, ...]] = {
            "mode": _MODES,
            "thinking_level": _THINKING_LEVELS,
            "voice": tuple(AVAILABLE_VOICES),
            "echo_gate": ("auto", "on", "off"),
            "frame_mode": _FRAME_MODES,
            "media_resolution": _MEDIA_RESOLUTIONS,
            "privacy_mode": _PRIVACY_MODES,
            "turn_coverage": tuple(_TURN_COVERAGES),
        }
        if name in choices:
            return self._choice_binding(choices[name])
        if name in ("model", "fallback_model"):
            return self._model_binding(optional=name == "fallback_model")
        if name == "input_device":
            return self._device_binding(self._inputs)
        if name == "output_device":
            return self._device_binding(self._outputs)
        if name == "audio_chunk_duration_ms":
            combo = self._combo([("20", 20), ("40", 40), ("100", 100)])
            return combo, _Binding(
                combo,
                combo.currentData,
                lambda v: combo.setCurrentIndex(max(0, combo.findData(v))),
            )
        int_ranges = {
            "vad_prefix_padding_ms": (0, 10_000),
            "vad_silence_duration_ms": (0, 10_000),
            "screen_monitor": (0, 16),
            "target_tokens": (0, 10_000_000),
            "target_tokens_tier1": (0, 10_000_000),
        }
        if name in int_ranges:
            spin = self._spin(*int_ranges[name])
            return spin, _Binding(spin, spin.value, lambda v: spin.setValue(int(v)))
        if name == "trigger_tokens":
            return self._trigger_binding()
        if name in ("screen_fps", "frame_heartbeat_seconds", "frame_min_interval_seconds"):
            dspin = self._dspin(0.1 if name == "screen_fps" else 0.0, 3600.0)
            return dspin, _Binding(dspin, dspin.value, lambda v: dspin.setValue(float(v)))
        if name in ("working_dir", "wake_word", "wake_word_aliases"):
            return self._line_binding(name)
        if name == "system_prompt":
            text = QPlainTextEdit()
            text.textChanged.connect(self._refresh_footer)
            return text, _Binding(
                text, lambda: text.toPlainText().strip(), text.setPlainText
            )
        check = QCheckBox()
        check.toggled.connect(self._refresh_footer)
        return check, _Binding(check, check.isChecked, check.setChecked)

    # -- data flow --

    def _load(self, settings: AppSettings) -> None:
        for name, binding in self._bindings.items():
            binding.set(getattr(settings, name))

    def build_settings(self) -> AppSettings:
        """Return validated settings; hidden fields (keys, debug, ...) are preserved."""
        values = {name: b.get() for name, b in self._bindings.items()}
        dumped = self._original.model_dump()
        dumped.update(values)
        working_dir = Path(str(values["working_dir"]).strip() or ".")
        dumped["working_dir"] = working_dir
        if working_dir.expanduser().resolve() != self._original.working_dir:
            dumped["workspace_root"] = working_dir
        dumped["headless"] = False
        dumped["debug"] = self._original.debug
        return AppSettings.model_validate(dumped)

    def changed_fields(self) -> set[str]:
        """Names of covered fields whose control value differs from the input settings."""
        changed: set[str] = set()
        for name, binding in self._bindings.items():
            current = binding.get()
            if name == "working_dir":
                with contextlib.suppress(OSError):
                    current = Path(str(current).strip() or ".").expanduser().resolve()
            elif name in ("model", "fallback_model") and current is not None:
                current = canonicalize_model_name(str(current))
            if current != getattr(self._original, name):
                changed.add(name)
        return changed

    def _refresh_footer(self, *_args: object) -> None:
        if not self._ready:
            return
        changed = self.changed_fields()
        live = len(changed & LIVE_FIELDS)
        reconnect = len(changed - LIVE_FIELDS)
        self._footer_label.setText(
            f"● {reconnect} changes apply on reconnect · {live} applied live"
        )

    def reset_current_section(self) -> None:
        """Restore the visible section's fields to AppSettings defaults."""
        _title, fields = _SECTIONS[self._stack.currentIndex()]
        for name in fields:
            if name in self._bindings:
                self._bindings[name].set(_field_default(name))
        self._refresh_footer()

    def _on_browse_working_dir(self) -> None:
        current = str(self._bindings["working_dir"].get())
        chosen = QFileDialog.getExistingDirectory(self, "Select working directory", current)
        if chosen:
            self._bindings["working_dir"].set(chosen)

    def _on_save(self) -> None:
        try:
            new_settings = self.build_settings()
        except ValidationError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        self.settings_saved.emit(new_settings)
        self.accept()

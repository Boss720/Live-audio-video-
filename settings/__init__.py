"""Application settings and secrets API."""

from __future__ import annotations

from settings.secrets import (
    get_api_key,
    get_free_api_key,
    migrate_dotenv,
    set_api_key,
    set_free_api_key,
)
from settings.settings import AVAILABLE_VOICES, AppSettings

__all__ = [
    "AVAILABLE_VOICES",
    "AppSettings",
    "get_api_key",
    "get_free_api_key",
    "migrate_dotenv",
    "set_api_key",
    "set_free_api_key",
]

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

CORE_VERSION = "0.4.1"
HOST = "127.0.0.1"
PORT = 8421
HISTORY_LIMIT = 100
SSE_HEARTBEAT_SECONDS = 15
VUE_URL = "https://cdn.jsdelivr.net/npm/vue@3.5.13/dist/vue.global.prod.js"


def data_directory() -> Path:
    value = os.environ.get("H3LOCAL_DATA_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else Path.home() / ".h3-ir-local"


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str = ""
    api_key: str | None = Field(
        None, description="Null keeps the session key; empty clears it. Never persisted.")
    timeout_s: float = Field(600, ge=5, le=3600, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or port == 0 or any(char.isspace() for char in value)):
            raise ValueError("Use an http(s) API base URL without credentials, query or fragment.")
        return value

    @field_validator("model")
    @classmethod
    def model_name(cls, value: str) -> str:
        return value.strip()

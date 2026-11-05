"""Configuration. Everything that could differ between one machine and another.

No host-specific path or URL is hardcoded anywhere else in the package. Every value comes
from the environment, and the defaults assume the two services are on this machine. If yours
are not — and the reasoning model usually is not — set `H3IR_LLM_URL` and `H3IR_COMFY_URL`.
See `.env.example`, and run `h3ir doctor` to find out what is actually answering.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    """An empty variable means "unset", not "the empty string".

    Otherwise a blank line in a sourced .env file silently overrides a default with nothing —
    `H3IR_STATE_DIR=` put the cache in the current working directory rather than under $HOME.
    """
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMConfig:
    """The reasoning/vision model. Verified: vision works, thinking lands in `reasoning`."""

    # Any OpenAI-compatible endpoint: vLLM, llama.cpp's server, LM Studio, Ollama, or a hosted
    # API. It needs a vision tower — reference images are analysed through it. The default is a
    # placeholder for "a server on this machine"; set H3IR_LLM_URL to yours.
    base_url: str = field(default_factory=lambda: _env("H3IR_LLM_URL", "http://127.0.0.1:8000/v1"))
    # Whatever `GET /v1/models` on your endpoint reports. `h3ir doctor` prints the list. Unset is
    # fine on a server with one model on it. On one that routes several the compiler refuses to
    # pick, because the model it needs is the one with a vision tower and no model list says which.
    model: str = field(default_factory=lambda: _env("H3IR_LLM_MODEL", ""))
    # Sent as `Authorization: Bearer`, unless it is left at this placeholder, in which case no
    # header goes out. See `Backend._headers` for why the placeholder is not sent as a credential.
    api_key: str = field(default_factory=lambda: _env("H3IR_LLM_KEY", "not-needed"))
    # Reasoning is the bulk of the spend and silently truncates the answer if starved.
    max_tokens: int = field(default_factory=lambda: _env_int("H3IR_LLM_MAX_TOKENS", 16000))
    timeout_s: float = field(default_factory=lambda: _env_float("H3IR_LLM_TIMEOUT", 600.0))
    # Measured: `enable_thinking: false` is the ONLY spelling that works, and json_schema
    # is not enforced while thinking is on. Structured stages must set thinking=False.
    # MEASURED OFF, against the paper's prediction, for this task specifically. arXiv:2606.09662
    # finds thinking helps PLANNING constraints -- but in this architecture the planning fields
    # (shot count, cut times, budgets, label scope) are owned by code too, so there is nothing
    # left for it to improve. Our own gate: thinking on the planning call cost 3.7x the wall time
    # (292s vs 78s over six briefs) and moved no quality metric; restatement got slightly worse.
    # The architecture that neutralises the penalty also neutralises the benefit.
    # `H3IR_LLM_THINKING=1` re-enables it; the beat-quality question it would help with is not
    # yet measurable without a render.
    default_thinking: bool = field(default_factory=lambda: _env_bool("H3IR_LLM_THINKING", False))
    # OFF by default, deliberately. With a reasoning parser active, vLLM gates grammar
    # enforcement on is_reasoning_end(), which for some configs never becomes true, so the
    # grammar is silently not applied (vLLM #39130, residual reports 2026-08). llama.cpp #20345
    # reports the converse on this exact model family: grammar can block `</think>` from ever
    # being emitted, looping until the token cap. We validate in Python either way, so the
    # grammar buys nothing we rely on and costs two known failure modes.
    guided_decoding: bool = field(default_factory=lambda: _env_bool("H3IR_GUIDED_DECODING", False))
    # Analysis copies only. The byte budget is shared across all images in one user turn.
    # Zero disables that limit; neither setting changes the original conditioning asset.
    vision_max_edge: int = field(
        default_factory=lambda: _env_int("H3IR_VISION_MAX_EDGE", 2048))
    vision_image_budget_bytes: int = field(
        default_factory=lambda: _env_int("H3IR_VISION_IMAGE_BUDGET_BYTES", 512 * 1024))


@dataclass(frozen=True)
class ComfyConfig:
    """ComfyUI is reached over HTTP. Never through the filesystem — do not assume this process
    shares a filesystem with the GPU box, because it usually will not."""

    base_url: str = field(default_factory=lambda: _env("H3IR_COMFY_URL", "http://127.0.0.1:8188"))
    timeout_s: float = field(default_factory=lambda: _env_float("H3IR_COMFY_TIMEOUT", 30.0))


@dataclass(frozen=True)
class AssetConfig:
    """Where an attachment's bytes may come from, and what an upload is allowed to cost.

    The service takes attachments two ways. A `path` it opens off its own filesystem is the
    original way and the cheap one: nothing is copied, and the bytes H3 receives are the bytes the
    caller dropped. It only works when the caller and the service see one disk. An upload is the
    other way, for the caller on another machine, and it is the one that spends the host's disk —
    so both of its ceilings are settings, and both are enforced against bytes actually received
    rather than against a header a caller can lie in.
    """

    # Per file. 512 MiB covers anything a camera or a renderer produces at the lengths H3 reads
    # (a 15-second 4K clip at 50 Mbps is about 94 MB), and it is small enough that one request
    # cannot take a disk. 0 turns uploads off entirely, which is the right setting for a service
    # that is only ever meant to read its own filesystem.
    upload_max_bytes: int = field(
        default_factory=lambda: max(0, _env_int("H3IR_UPLOAD_MAX_BYTES", 512 * 1024 * 1024)))
    # The whole store. Uploads accumulate across requests, so a per-file limit alone bounds nothing
    # over a week. Reaching this evicts the least recently used assets; if that cannot free enough,
    # the upload is refused rather than the cap being quietly exceeded.
    upload_store_bytes: int = field(
        default_factory=lambda: max(0, _env_int("H3IR_UPLOAD_STORE_BYTES", 8 * 1024 * 1024 * 1024)))
    # Uploaded bytes are a cache, not a record: every one of them is reproducible by uploading
    # again. 0 means keep them until the size cap needs the room.
    upload_ttl_hours: int = field(
        default_factory=lambda: max(0, _env_int("H3IR_UPLOAD_TTL_HOURS", 48)))
    # `path` attachments, ON by default because that is the behaviour every existing caller has and
    # the fast path on one machine. Turn it OFF on a service reachable by anyone you would not hand
    # a shell to: a path is read with the service's own permissions, and a caller who can name a
    # file can have its contents described back to them through the vision model. With this off the
    # only bytes the service reads are bytes a caller sent it.
    allow_paths: bool = field(default_factory=lambda: _env_bool("H3IR_ALLOW_ASSET_PATHS", True))


@dataclass(frozen=True)
class Paths:
    state_dir: Path = field(default_factory=lambda: Path(
        _env("H3IR_STATE_DIR", str(Path.home() / ".local/share/h3ir"))))
    # os.pathsep-separated. Empty by default rather than pointing somewhere that does not exist:
    # `h3ir loras` on a fresh install should report an empty registry, not a missing directory.
    # The worked example in the repo is at `./loras`; the format is design.md §14.
    lora_dirs: tuple[Path, ...] = field(default_factory=lambda: tuple(
        Path(p) for p in _env("H3IR_LORA_DIRS", str(Path.home() / ".local/share/h3ir/loras"))
        .split(os.pathsep) if p))
    tokenizer_dir: Path = field(default_factory=lambda: Path(
        _env("H3IR_TOKENIZER_DIR", str(PKG_DIR / "data/qwen25_tokenizer"))))
    # Inside the package, so an installed copy finds them without any environment set up.
    spec_dir: Path = field(default_factory=lambda: Path(
        _env("H3IR_SPEC_DIR", str(PKG_DIR / "prompts"))))
    golden_dir: Path = field(default_factory=lambda: Path(
        _env("H3IR_GOLDEN_DIR", str(PKG_DIR / "golden"))))

    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    def eval_dir(self) -> Path:
        return self.state_dir / "eval"

    def uploads_dir(self) -> Path:
        """Bytes callers sent. Under the state dir with the caches because it IS one: everything in
        it is reproducible by uploading again, so it is safe to delete at any time."""
        return self.state_dir / "uploads"


@dataclass(frozen=True)
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    comfy: ComfyConfig = field(default_factory=ComfyConfig)
    paths: Paths = field(default_factory=Paths)
    assets: AssetConfig = field(default_factory=AssetConfig)
    profile: str = field(default_factory=lambda: _env("H3IR_PROFILE", "h3ir/2026-08-a"))
    # If the LLM is unreachable we fail loudly rather than quietly producing a worse IR:
    # a caller cannot tell a good IR from a bad one.
    allow_degraded: bool = field(default_factory=lambda: _env_bool("H3IR_ALLOW_DEGRADED", False))


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def set_config(cfg: Config) -> None:
    """Test seam."""
    global _CONFIG
    _CONFIG = cfg

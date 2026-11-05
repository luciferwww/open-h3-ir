"""LLM client, with the endpoint's measured failure modes handled once, here.

Three things were measured against the live endpoint and each one fails SILENTLY if you
don't handle it. They are handled in this wrapper so no stage has to remember:

  F15  It is a thinking model and vLLM returns the thinking in a separate `reasoning`
       field. With a small budget, reasoning consumes it all and `content` comes back
       None with finish_reason 'length' and NO error. -> we raise.
       Only `chat_template_kwargs: {"enable_thinking": false}` suppresses it;
       `{"thinking": false}` is accepted and silently ignored.

  F16  `response_format: json_schema` with strict:true is NOT APPLIED while thinking is
       enabled. A strict AssetCard schema came back as an ASCII-art table, finish_reason
       'stop', no error. -> structured calls force thinking off.

  F17  Even with thinking off the grammar constrains shape, not completion or sense: a
       string can run to max_tokens and return unterminated, unparseable JSON. -> we
       check finish_reason, parse, and re-validate required keys ourselves.

The other thing this file owns is that "OpenAI-compatible" is a family, not a contract. The
servers below all speak `/v1/chat/completions` and disagree about everything around it: where
liveness lives, whether the model list carries a context length, whether one id means one model,
and whether a credential is wanted. Each of those is handled here, once, against what the server
itself publishes rather than against the one endpoint this was built on. See `health_probe`,
`_discover_model` and `_headers`.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import struct
import tempfile
import time
import warnings
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from .config import LLMConfig, get_config

log = logging.getLogger("h3ir.backend")

# Ceiling for the truncation-retry ladder, so a looping model cannot multiply the wait.
MAX_GROWTH_BASE = 16000


class BackendError(RuntimeError):
    """Raised loudly. We never quietly downgrade: a caller cannot tell a good IR from a
    bad one, so silently halving quality is the failure nobody notices."""


class BackendUnavailable(BackendError):
    pass


class TruncatedResponse(BackendError):
    pass


class EndpointRefused(BackendError):
    """The server answered with an error status.

    Separate from the other two because it is a JUDGEMENT the server made about the request, and
    that is information rather than only a failure. `h3ir doctor` reads a 400 on an image as an
    answer about the model, which is how a text-only model gets named as one instead of printing
    a stack of escaped JSON at somebody.
    """

    def __init__(self, status: int, body: str, *, request_bytes: int | None = None):
        self.status = status
        self.body = body
        self.request_bytes = request_bytes
        message = f"HTTP {status}: {_server_sentence(body)[:400]}"
        if status == 413 and request_bytes is not None:
            message += (
                f" (outgoing JSON: {request_bytes} bytes; the model endpoint or proxy rejected "
                "the request as too large. For vision requests, lower "
                "H3IR_VISION_IMAGE_BUDGET_BYTES; otherwise shorten the request or increase "
                "the endpoint's body limit.)")
        super().__init__(message)


def _server_sentence(body: str) -> str:
    """The human sentence inside an error body, however many times it was wrapped.

    Every server here answers an error with JSON and no two agree on the shape. Ollama nests a
    whole JSON document inside `error.message` as a STRING, so the one sentence a reader needs
    ("model does not support multimodal requests") arrives escaped twice and reads as noise. Dug
    out rather than printed raw, because an error nobody can read is the same as no error. Falls
    back to the body untouched, since a wrong guess about the shape must not lose the evidence.
    """
    text = (body or "")[:8000]
    for _ in range(4):          # bounded: each pass peels one layer of encoding
        try:
            obj = json.loads(text)
        except ValueError:
            return text.strip()
        if isinstance(obj, str):
            text = obj
            continue
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, str):
                text = err
                continue
            if isinstance(err, dict):
                obj = err
            msg = obj.get("message") or obj.get("detail")
            if isinstance(msg, str):
                text = msg
                continue
        return (body or "").strip()
    return text.strip()


@dataclass
class Reply:
    content: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    wall_s: float
    model: str

    @property
    def reasoning_share(self) -> float:
        if not self.completion_tokens:
            return 0.0
        # crude but useful: reasoning chars vs content chars
        total = len(self.reasoning) + len(self.content)
        return len(self.reasoning) / total if total else 0.0


@dataclass(frozen=True)
class HealthProbe:
    """Whether the endpoint answers, and what every path that was tried said.

    `via` is the point of it. Liveness is where a reader's debugging starts, the answer differs by
    server, and `h3ir doctor` exists to tell somebody the truth about the setup in front of them:
    "healthy" without naming which path replied is a claim they cannot check.
    """

    ok: bool
    via: str
    attempts: tuple[tuple[str, str], ...]


def image_data_url(path: str | Path, *, max_bytes: int | None = None,
                   max_edge: int | None = None) -> str:
    """Bound the analysis copy, not the original asset. Declare the actual encoded MIME type."""
    cfg = get_config().llm
    max_bytes = cfg.vision_image_budget_bytes if max_bytes is None else max_bytes
    max_edge = cfg.vision_max_edge if max_edge is None else max_edge
    if max_bytes < 0 or max_edge < 0:
        raise BackendError("vision image limits must be non-negative; zero disables a limit")
    p = Path(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(p) as original:
                size = p.stat().st_size
                dimensions = original.size
                resize = bool(max_edge and max(dimensions) > max_edge)
                if not resize and (not max_bytes or size <= max_bytes):
                    mime = Image.MIME.get(original.format or "")
                    if not mime:
                        raise BackendError(f"unsupported vision image format: {original.format}")
                    data = p.read_bytes()
                else:
                    if max_edge:
                        original.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    image = ImageOps.exif_transpose(original)
                    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        image = image.convert("RGB")
                    while True:
                        for quality in (90, 80, 70):
                            with io.BytesIO() as output:
                                image.save(output, format="JPEG", quality=quality, optimize=True)
                                data = output.getvalue()
                            if not max_bytes or len(data) <= max_bytes:
                                break
                        else:
                            if image.size == (1, 1):
                                raise BackendError(
                                    f"vision image budget ({max_bytes} bytes) cannot fit a JPEG")
                            image = image.resize(
                                (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                                Image.Resampling.LANCZOS)
                            continue
                        break
                    mime = "image/jpeg"
                    log.warning("vision analysis preview %s: %sx%s, %s bytes -> %sx%s, %s "
                                "bytes; original asset unchanged", p.name, *dimensions, size,
                                *image.size, len(data))
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise BackendError(f"cannot prepare vision image {p.name}: {exc}") from exc
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def user_message(text: str, images: list[str | Path] | None = None, *,
                 llm: LLMConfig | None = None) -> dict[str, Any]:
    """Multipart user turn. Verified: this endpoint reads images and can attribute several
    of them correctly within one request."""
    if not images:
        return {"role": "user", "content": text}
    cfg = llm or get_config().llm
    budget = cfg.vision_image_budget_bytes
    if budget < 0 or cfg.vision_max_edge < 0:
        raise BackendError("vision image limits must be non-negative; zero disables a limit")
    if budget and budget < len(images):
        raise BackendError("vision image budget is too small for the number of images")
    per_image = budget // len(images) if budget else 0
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for i, img in enumerate(images, 1):
        parts.append({"type": "text", "text": f"Image {i}:"})
        parts.append({"type": "image_url", "image_url": {
            "url": image_data_url(img, max_bytes=per_image, max_edge=cfg.vision_max_edge)}})
    return {"role": "user", "content": parts}


class Backend:
    def __init__(self, cfg=None, client: httpx.Client | None = None):
        self.cfg = (cfg or get_config()).llm
        self._client = client
        self._owns_client = client is None
        self._resolved_model: str | None = None

    def model_id(self) -> str:
        """The model id to send. Set by config, or discovered by `require_available`."""
        return self.cfg.model or self._resolved_model or ""

    def _discover_model(self) -> None:
        """Ask the endpoint what it serves, and take an id only when there is no choice in it.

        Only called from `require_available`, i.e. only on a path that is about to talk to a real
        server. On a server with one model on it there is no decision to make and requiring its
        full name would be configuration with nothing in it, so the id is taken.

        On an endpoint that routes several models, taking the first one is a guess, and it is a
        guess about a property the model list does not carry. This compiler reads reference images
        through the endpoint, so it needs the model with a vision tower, and `GET /v1/models`
        reports vision on none of these servers: Ollama's entries carry `id`, `object`, `created`
        and `owned_by` and nothing else. Reported from a real Ollama install, where the first id
        was a large text-only coding model, every reference image went unread, and nothing said so.
        So it refuses, and names what it found.

        Several ids are not necessarily several models. vLLM's `--served-model-name` publishes one
        set of weights under several ids and gives every entry the same `root`, so entries are
        counted by `root` where there is one. Where there is none, as on Ollama, each id counts as
        its own model, which is the safe direction: it can only make this refuse, never guess.
        """
        try:
            data = self._get(f"{self.base_url()}/models", 10.0).json()
            entries = [m for m in (data.get("data") or []) if m.get("id")]
        except Exception as e:  # noqa: BLE001 - re-raised as a BackendError below
            raise BackendUnavailable(
                f"H3IR_LLM_MODEL is not set and {self.base_url()}/models could not be read to "
                f"discover it: {e}") from e
        ids = [str(m["id"]) for m in entries]
        if not ids:
            raise BackendUnavailable(
                f"H3IR_LLM_MODEL is not set and {self.base_url()}/models named no models. "
                "Set H3IR_LLM_MODEL to the id your endpoint serves.")
        distinct = {m.get("root") or m["id"] for m in entries}
        if len(distinct) > 1:
            raise BackendUnavailable(
                f"H3IR_LLM_MODEL is not set and {self.base_url()}/models names {len(ids)} model "
                f"ids, so which one to use is a real choice and this will not guess it. It reads "
                f"reference images, and a model list does not say which model can see. "
                f"Set H3IR_LLM_MODEL to one of: {', '.join(ids)}. Then run `h3ir doctor`, which "
                f"reads a test image through the model you chose and reports whether it saw it.")
        if len(ids) > 1:
            log.info("H3IR_LLM_MODEL is not set; %d ids resolve to one model, using %r",
                     len(ids), ids[0])
        self._resolved_model = ids[0]

    def _headers(self) -> dict[str, str]:
        """Bearer auth, when a credential was actually configured.

        `H3IR_LLM_KEY` existed as a setting and was never sent anywhere, which is invisible against
        a local server that wants no credential and is a 401 against every endpoint that does.

        The default is the literal placeholder from `.env.example`, and sending THAT as a
        credential would be worse than sending nothing: a server with auth on would reject it as a
        bad key rather than a missing one, and the message a user has to debug would name the wrong
        problem. So the placeholder means "no header".
        """
        key = (self.cfg.api_key or "").strip()
        if not key or key == "not-needed":
            return {}
        return {"Authorization": f"Bearer {key}"}

    def _get(self, url: str, timeout: float) -> httpx.Response:
        """Every GET this file makes, so a configured credential is on all of them."""
        return self._http().get(url, timeout=timeout, headers=self._headers())

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ health

    def base_url(self) -> str:
        """The configured API base, with any trailing slash taken off.

        `H3IR_LLM_URL=http://host:11434/v1/` is an ordinary thing for somebody to paste out of a
        browser, and left alone it sends every request to `/v1//chat/completions`, which Starlette
        answers with a 404 that says nothing about the extra slash.
        """
        return self.cfg.base_url.rstrip("/")

    def _root_url(self) -> str:
        """The server root, for the paths that sit beside `/v1` rather than under it.

        The suffix is removed rather than cut at the last `/v1` anywhere in the string, because a
        gateway can mount the OpenAI surface under a path: `http://gw/v1/openai` cut that way
        leaves `http://gw`, and the liveness check then asks a different service entirely.
        """
        base = self.base_url()
        return base[: -len("/v1")] if base.endswith("/v1") else base

    def liveness_urls(self) -> tuple[str, ...]:
        """The paths tried, in order, to decide whether an endpoint is up.

        There is no standard one, and every server this project claims to support puts it
        somewhere else. Read off each server's own routing table rather than assumed:

          | server                  | liveness            |
          |-------------------------|---------------------|
          | vLLM, llama.cpp, SGLang | `/health`           |
          | Ollama                  | no `/health` at all |
          | LM Studio, hosted APIs  | `/v1/models`        |

        Ollama's `server/routes.go` registers `/`, `/api/version`, `/v1/models` and the inference
        paths, and nothing named health, so asking it for `/health` returns 404 forever. Asking
        only there is what made `h3ir doctor` report a working Ollama as unreachable.

        `/v1/models` is first because it is the surface every call in this file goes through and
        all of those servers serve it, so the ordinary case is one request and the answer is about
        the API rather than about the process. `/health` is second because a gateway can expose
        chat completions without a model list, and an endpoint answering only there is still usable
        once `H3IR_LLM_MODEL` is set. Either one answering means up, which is the reporter's own
        suggestion and the right one.

        The server root itself is deliberately NOT tried. Ollama answers `/` with "Ollama is
        running", but so does every unrelated web server on that port, and a liveness check that
        passes against a wrong port is worse than one that fails.
        """
        return (f"{self.base_url()}/models", f"{self._root_url()}/health")

    def health_probe(self) -> HealthProbe:
        """Which liveness path answered, and what each one said. See `liveness_urls`."""
        attempts: list[tuple[str, str]] = []
        for url in self.liveness_urls():
            try:
                r = self._get(url, 5.0)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # The socket never opened, so the path was never the question and the second one
                # would spend another timeout learning the same thing. Stopping here keeps the
                # wrong-port case as fast as it was when only one path was tried.
                attempts.append((url, f"{type(e).__name__}: {e}"))
                break
            except httpx.HTTPError as e:
                attempts.append((url, f"{type(e).__name__}: {e}"))
                continue
            attempts.append((url, f"HTTP {r.status_code}"))
            if r.status_code == 200:
                return HealthProbe(True, url, tuple(attempts))
        return HealthProbe(False, "", tuple(attempts))

    def health(self) -> bool:
        return self.health_probe().ok

    def server_version(self) -> str:
        """Recorded with every render. This bug family is version-dependent, so an upgrade
        invalidates the assumptions in this file.

        Two spellings, for the same reason liveness has two: vLLM and llama.cpp answer `/version`,
        Ollama answers `/api/version`, and both return `{"version": ...}`. Provenance that reads
        `?` on every run is provenance nobody can use.
        """
        for url in (f"{self._root_url()}/version", f"{self._root_url()}/api/version"):
            try:
                v = self._get(url, 5.0).json().get("version")
            except Exception:  # noqa: BLE001 - provenance only
                continue
            if v:
                return str(v)
        return "?"

    def require_available(self) -> None:
        hp = self.health_probe()
        if not hp.ok:
            tried = "; ".join(f"{u} -> {w}" for u, w in hp.attempts)
            raise BackendUnavailable(
                f"the reasoning model at {self.base_url()} is not reachable. "
                "Start it, or set H3IR_LLM_URL. Refusing to produce a lower-quality IR silently. "
                f"Tried: {tried}")
        if not self.cfg.model and self._resolved_model is None:
            self._discover_model()

    # ------------------------------------------------------------------ calls

    def chat(self, messages: list[dict[str, Any]], *, thinking: bool | None = None,
             max_tokens: int | None = None, temperature: float = 0.7,
             seed: int | None = None, response_format: dict[str, Any] | None = None,
             stop: list[str] | None = None, retries: int = 2) -> Reply:
        if thinking is None:
            thinking = self.cfg.default_thinking
        # F16: a schema is only honoured with thinking off. Refuse the unsafe combination
        # rather than returning prose that looks like a schema failure nobody notices.
        if response_format is not None and thinking:
            raise BackendError(
                "structured output requires thinking=False on this endpoint "
                "(json_schema is silently not applied while reasoning is enabled)")

        body: dict[str, Any] = {
            "model": self.model_id(),
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "temperature": temperature,
        }
        if seed is not None:
            body["seed"] = seed
        if stop:
            body["stop"] = stop
        if response_format is not None:
            body["response_format"] = response_format
        if not thinking:
            # The ONLY spelling that works. {"thinking": False} is silently ignored.
            body["chat_template_kwargs"] = {"enable_thinking": False}

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._once(body)
            except (TruncatedResponse, httpx.HTTPError) as e:
                last = e
                if attempt < retries:
                    # Growing the budget is right when reasoning ate it, and wrong when the
                    # model is looping -- there, each retry multiplies the wasted time. So the
                    # ladder is capped rather than open-ended.
                    if isinstance(e, TruncatedResponse):
                        grown = int(body["max_tokens"] * 1.75)
                        ceiling = max(self.cfg.max_tokens, 2 * MAX_GROWTH_BASE)
                        if grown > ceiling:
                            log.warning("truncated at %s tokens and the ceiling is %s; not "
                                        "growing further", body["max_tokens"], ceiling)
                            break
                        body["max_tokens"] = grown
                        log.warning("truncated; retrying with max_tokens=%s", body["max_tokens"])
                    else:
                        time.sleep(1.5 * (attempt + 1))
                    continue
                break
        # Re-raise the original so callers can still see WHY it failed. TruncatedResponse is a
        # BackendError, so `except BackendError` in the compiler still catches it and falls back.
        if isinstance(last, BackendError):
            raise last
        raise BackendError(f"chat failed after {retries + 1} attempt(s): {last}") from last

    def _once(self, body: dict[str, Any]) -> Reply:
        t0 = time.time()
        r = self._http().post(f"{self.base_url()}/chat/completions", json=body,
                              headers=self._headers())
        wall = time.time() - t0
        if r.status_code >= 400:
            raise EndpointRefused(r.status_code, r.text, request_bytes=len(r.request.content))
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or "?"

        # F15: reasoning ate the budget. Loud, not empty-string.
        #
        # The cause is only named when the reply carries the evidence for it. This message used to
        # assert the reasoning budget every time, and a small non-reasoning model that simply
        # stopped produced "the reasoning budget was consumed" beside "reasoning_chars=0" in the
        # same sentence: a diagnosis its own numbers contradict, sending the reader to raise a
        # budget that was never the problem.
        if content is None or not str(content).strip():
            why = ("the reasoning budget was consumed before any answer was emitted" if reasoning
                   else "no reasoning came back either, so the budget is not the explanation and "
                        "this model stopped without answering")
            raise TruncatedResponse(
                f"model returned no content (finish_reason={finish}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"reasoning_chars={len(reasoning)}): {why}")
        if finish == "length":
            raise TruncatedResponse(
                f"response hit max_tokens ({body['max_tokens']}); output is incomplete")

        # vLLM #35221: when generation truncates before `</think>`, the Qwen3 reasoning parser
        # cannot tell "thinking off" from "thinking unfinished" and puts raw in-progress
        # reasoning into `content`. An unclosed marker means this reply is reasoning, not an
        # answer, so trust the marker rather than the field split.
        text = str(content)
        if "<think>" in text and "</think>" not in text:
            raise TruncatedResponse(
                "content carries an unclosed <think> — this is leaked in-progress reasoning, "
                "not an answer (vLLM #35221)")

        return Reply(content=str(content).strip(), reasoning=reasoning,
                     prompt_tokens=usage.get("prompt_tokens", 0),
                     completion_tokens=usage.get("completion_tokens", 0),
                     finish_reason=finish, wall_s=wall, model=data.get("model", ""))

    # ------------------------------------------------------------------ structured

    def json_call(self, messages: list[dict[str, Any]], schema: dict[str, Any], *,
                  required: tuple[str, ...] = (), max_tokens: int | None = None,
                  seed: int | None = None, temperature: float = 0.0,
                  thinking: bool | None = None, retries: int = 2) -> dict[str, Any]:
        """A structured call that never trusts the grammar.

        Two modes, and the default is the one without grammar. With guided decoding off we ask
        for JSON in the prompt and extract it, which also allows thinking=True -- and thinking
        is what the planning call wants (arXiv:2606.09662: +5.3pp on planning constraints).
        With it on, the schema is a hint only; the shape is still checked here, because the
        grammar constrains shape but neither completion nor sense.
        """
        use_grammar = self.cfg.guided_decoding
        if thinking is None:
            # OFF unless a caller asks. arXiv:2505.11423's effective mitigation is
            # classifier-selective reasoning -- think for the PLANNING call, not for emission or
            # extraction calls, where thinking measurably hurts precision constraints (-8.5pp)
            # and costs a budget these small calls do not have.
            thinking = False
        rf = None
        msgs = list(messages)
        if use_grammar:
            thinking = False        # grammar + reasoning parser is the unsafe combination
            rf = {"type": "json_schema",
                  "json_schema": {"name": schema.get("title", "Result"), "schema": schema,
                                  "strict": True}}
        else:
            msgs[-1] = dict(msgs[-1])
            msgs[-1]["content"] = _append_text(msgs[-1]["content"], _schema_ask(schema))
        last: Exception | None = None
        for attempt in range(retries + 1):
            # A retry has to ask a DIFFERENT question, and at temperature 0 with a fixed seed it
            # cannot: the decode is deterministic, so re-sending identical messages returns the
            # identical reply. Measured, not theorised -- three attempts produced three
            # byte-identical 524-token replies in which the model echoed the schema instead of
            # filling it, on a vision call whose only unusual feature was an appended caller note.
            # Temperature demonstrably breaks that tie where the seed does not, because at
            # temperature 0 the seed changes nothing. So attempt 0 keeps the caller's temperature,
            # which keeps the common path reproducible and cacheable, and only the retries move.
            # The first retry jumps straight to 0.7, which is the value measured to break
            # this tie, rather than creeping up from 0.4 and wasting an attempt at a
            # temperature that was never shown to work. Later attempts climb from there.
            temp = temperature if attempt == 0 else max(0.7, temperature + 0.3) + 0.3 * (attempt - 1)
            reply = self.chat(msgs, thinking=thinking, response_format=rf,
                              max_tokens=max_tokens, temperature=temp, seed=seed,
                              retries=1)
            try:
                obj = json.loads(_extract_json(reply.content))
            except json.JSONDecodeError as e:
                last = BackendError(f"schema-shaped output did not parse: {e}: "
                                    f"{reply.content[:200]!r}")
                log.warning("%s", last)
                continue
            if not isinstance(obj, dict):
                last = BackendError(f"expected a JSON object, got {type(obj).__name__}")
                continue
            missing = [k for k in required if k not in obj]
            if missing:
                if _is_the_schema_itself(obj):
                    last = BackendError(
                        f"the model returned the schema document instead of an instance of it "
                        f"(missing {missing}); retrying at a higher temperature")
                else:
                    how = ("strict schema" if use_grammar
                           else "schema asked for in the prompt, not enforced by a grammar")
                    last = BackendError(f"required key(s) absent, {how}: {missing}")
                log.warning("%s", last)
                continue
            return obj
        raise BackendError(f"structured call failed after {retries + 1} attempt(s): {last}")


def _is_the_schema_itself(obj: dict[str, Any]) -> bool:
    """True when the reply is the schema document rather than an instance of it.

    A valid-JSON failure, which is why it survives parsing and shows up as absent keys. Seen on a
    vision call where an appended caller note tipped a greedy decode into copying the schema back:
    the object's keys were `title`, `type`, `properties`, `required`, `additionalProperties`.
    Naming it turns a confusing "required key absent" into a message that says what happened.
    """
    return obj.get("type") == "object" and isinstance(obj.get("properties"), dict)


def _schema_ask(schema: dict[str, Any]) -> str:
    return ("\n\nReturn ONE JSON object and nothing else — no prose before or after it, "
            "no code fence. It must match this schema:\n" + json.dumps(schema, indent=1))


def _append_text(content: Any, extra: str) -> Any:
    """Append an instruction without destroying a multimodal message.

    A multipart `content` is a LIST of parts, one of which holds a base64 data URL. Coercing it
    with str() stringifies the whole list -- including the entire image as literal text -- which
    silently explodes the prompt by orders of magnitude and looks like the endpoint hanging. It
    is not a hypothetical: it cost ten minutes of a stalled acceptance run to find.
    """
    if isinstance(content, list):
        return list(content) + [{"type": "text", "text": extra}]
    return str(content) + extra


def _extract_json(text: str) -> str:
    """Pull the object out of a reply that may carry a fence or a sentence around it."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    if t.startswith("{"):
        return t
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return t[start:end + 1]
    return t


# --------------------------------------------------------------------- the vision self-test

# Three digits, so a text-only model cannot land on them by chance, and fixed so two runs of
# `h3ir doctor` are the same question.
VISION_PROBE_DIGITS = "473"

# A 5x7 cell per digit. A blockier 3x5 font is smaller and is also where a legible-enough glyph
# stops being obvious, and a vision check that fails on its own typography would be reporting on
# this file rather than on the endpoint.
_DIGIT_GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def digits_png(digits: str, *, scale: int = 16, pad: int = 16) -> bytes:
    """A greyscale PNG of `digits`, black on white, drawn here rather than shipped as a blob.

    No image library: this is one dependency the compiler does not otherwise need, and a picture a
    reader can regenerate and diff is worth more here than a binary file they have to trust.
    """
    glyphs = [_DIGIT_GLYPHS[d] for d in digits]
    gw, gh = 5 * scale, 7 * scale
    gap = scale
    w = 2 * pad + len(glyphs) * gw + (len(glyphs) - 1) * gap
    h = 2 * pad + gh
    rows = []
    for y in range(h):
        row = bytearray(b"\xff" * w)
        gy = (y - pad) // scale
        if 0 <= gy < 7:
            for i, g in enumerate(glyphs):
                x0 = pad + i * (gw + gap)
                for gx, cell in enumerate(g[gy]):
                    if cell == "1":
                        row[x0 + gx * scale: x0 + (gx + 1) * scale] = b"\x00" * scale
        rows.append(b"\x00" + bytes(row))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _png_chunk(b"IEND", b""))


def vision_check(b: Backend) -> tuple[bool, str]:
    """Ask the configured model to read three digits off a generated picture.

    The one capability this compiler cannot do without, and until now the one thing nothing
    checked. A text-only model answers the `chat_ok` probe perfectly and then reads no reference
    image at all, which is exactly what the first Ollama report was: the endpoint was up, the
    model was fine, and it could not see. `doctor` is where somebody looks when they are confused,
    so the question belongs here.

    The image is written to a file and passed through `user_message`, so this exercises the
    production wiring including the mime sniff, rather than a shortcut built for the check.
    """
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(digits_png(VISION_PROBE_DIGITS))
        reply = b.chat(
            [user_message(f"This picture shows {len(VISION_PROBE_DIGITS)} digits. "
                          "Reply with only those digits.", [path], llm=b.cfg)],
            # 256 rather than a tight budget: a captioning model answers a question about a
            # picture with a sentence, and a check that fails on verbosity would be reporting on
            # the model's manners rather than on whether it saw anything. Measured: moondream on
            # Ollama truncated at 64 and produced no verdict at all.
            thinking=False, max_tokens=256, temperature=0.0, retries=0)
    finally:
        os.unlink(path)
    seen = "".join(c for c in reply.content if c.isdigit())
    return VISION_PROBE_DIGITS in seen, reply.content.strip()[:160]


# The statuses that mean "I looked at this request and it is not acceptable". Those are the only
# ones that say anything about whether a model can read a picture, and a text-only model on a strict
# server answers with one of them.
JUDGED_THE_REQUEST = (400, 415, 422)


def judges_the_picture(status: int) -> bool:
    """Did the server judge the picture, or was the model never asked?

    **A refused request is not automatically an answer about vision**, and reading it as one is the
    wrong-message failure this whole file is written against. MEASURED against a live vLLM: asking
    about a model the endpoint does not serve answers `HTTP 404: The model does not exist`, and
    reporting that as `vision_ok: False` sends somebody hunting for a model with a vision tower to
    replace one that was never there.

    Four things get refused and only one of them is a verdict:

        400, 415, 422   the server read the request and rejected the picture. That IS the answer,
                        and it is what a text-only model on a strict server does.
        404             there is no such model here. Nothing was asked.
        401, 403        the server wants a credential. Nothing was asked.
        anything else   the server broke, or something in front of it did. Nothing was asked.

    The same rule holds the ComfyUI node pack's own vision route, which is where it was found.
    """
    return status in JUDGED_THE_REQUEST


def _why_nothing_was_asked(status: int, model: str) -> str:
    """What really happened, for the three refusals that are not about vision.

    Each one has its own fix, and a reader handed the wrong one goes and repairs something that is
    working.
    """
    if status == 404:
        return (f"this endpoint has no model called {model!r}, so nothing was asked and nothing is "
                "known about whether it can see. Check H3IR_LLM_MODEL against the ids above.")
    if status in (401, 403):
        return (f"this endpoint refused the request as unauthorised, so {model!r} was never asked. "
                "It wants a credential. Set H3IR_LLM_KEY.")
    return (f"this endpoint answered with an error rather than a verdict, so nothing is known about "
            f"whether {model!r} can see. That is about the server rather than about the model.")


def probe(cfg=None) -> dict[str, Any]:
    """Report what the endpoint is and what it supports. Used by `h3ir doctor`.

    Every line here is a fact somebody has had to work out the hard way, so each one says what
    answered and where, never just that something was fine.
    """
    c = cfg or get_config()
    out: dict[str, Any] = {"url": c.llm.base_url}
    with Backend(c) as b:
        # Whether, never what. Somebody debugging a 401 needs to know if a credential went out at
        # all, and a key printed here would be a key in a terminal, a scrollback and a bug report.
        out["credential"] = "Authorization: Bearer sent" if b._headers() else "none sent"
        hp = b.health_probe()
        out["health"] = hp.ok
        out["health_via"] = hp.via or "(nothing answered)"
        out["health_tried"] = "; ".join(f"{u} -> {w}" for u, w in hp.attempts)
        if not hp.ok:
            return out
        ids: list[str] = []
        try:
            r = b._get(f"{b.base_url()}/models", 10.0).json()
            entries = [m for m in (r.get("data") or []) if m.get("id")]
            ids = [str(m["id"]) for m in entries]
            out["model_ids"] = ids
            # vLLM publishes it here; Ollama's model objects carry id, object, created and
            # owned_by only. Absent is not small, and printing None invites the wrong repair.
            out["max_model_len"] = next(
                (m.get("max_model_len") for m in entries if m.get("max_model_len")),
                "(this endpoint does not report one)")
        except Exception as e:  # noqa: BLE001 - diagnostics only
            out["models_error"] = str(e)
        if c.llm.model:
            out["model"] = c.llm.model
            out["model_from"] = "H3IR_LLM_MODEL"
            if ids and c.llm.model not in ids:
                out["model_warning"] = (
                    f"H3IR_LLM_MODEL is {c.llm.model!r} and this endpoint does not list it. "
                    "Every call will fail. Use one of the ids above.")
        else:
            try:
                b._discover_model()
                out["model"] = b.model_id()
                out["model_from"] = ("H3IR_LLM_MODEL is unset and this endpoint serves one model, "
                                     "so there was nothing to choose")
            except BackendError as e:
                out["model"] = "(none: H3IR_LLM_MODEL is unset and this will not guess)"
                out["model_from"] = str(e)
                return out
        try:
            reply = b.chat([{"role": "user", "content": "Reply with the single word: ready"}],
                           thinking=False, max_tokens=32, temperature=0.0)
            out["chat_ok"] = "ready" in reply.content.lower()
            out["latency_s"] = round(reply.wall_s, 2)
        except BackendError as e:
            # Not a return: a model that fails the word-back instruction may still read a picture,
            # and which of the two failed is the thing the reader came here to find out.
            out["chat_error"] = str(e)
        try:
            ok, said = vision_check(b)
        except EndpointRefused as e:
            # A refused request is not automatically an answer about vision. See `judges_the_picture`
            # for which statuses are and which are not.
            if not judges_the_picture(e.status):
                out["vision_error"] = str(e)
                out["vision_note"] = _why_nothing_was_asked(e.status, b.model_id())
                return out
            ok, said = False, str(e)
        except BackendError as e:
            # A timeout or a truncation says nothing either way, so no verdict is recorded.
            out["vision_error"] = str(e)
            return out
        out["vision_ok"] = ok
        out["vision_reply"] = said
        if not ok:
            out["vision_note"] = (
                "the model did not read the test picture, so it has no vision tower or cannot use "
                "it. Reference images are read through this model, so a brief with a picture "
                "attached cannot work. Point H3IR_LLM_MODEL at a model that can see.")
    return out

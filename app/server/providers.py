"""Run the pipeline against providers other than Anthropic.

``autofab.agents`` reaches the network through exactly one function,
``_get_client()``, and every agent - Planner, Coder, Error Refiner, Refiner
and the vision Judge - goes through it.  Supplying a client that presents the
same surface is therefore enough to retarget the whole pipeline, with no
change to the research code.

Two backends cover the field:

* **anthropic** - the real SDK client, returned untouched.  Selecting the
  default provider changes nothing about how the pipeline runs.
* **openai_compatible** - a shim over ``/chat/completions``.  Because the base
  URL is configurable this one adapter serves OpenAI, Ollama, llama.cpp's
  server, vLLM, LM Studio and the hosted gateways that speak the same API, so
  a local Llama needs no special case.

The Judge is told apart from the generation agents by its system prompt
(``agents.VALIDATOR_SYSTEM``) rather than by the model id hardcoded at the
call site, so the two roles can be pointed at different models - even
different providers - and the pipeline's deliberate use of an independent
judge survives.

Keys come from the environment or from a per-session override held only in
memory.  Nothing here writes a key to disk, logs one, or returns one to the
browser.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

#: Generous by design: a local model on CPU can take minutes for one reply.
REQUEST_TIMEOUT = float(os.getenv("CADSMITH_LLM_TIMEOUT", "600"))


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str                     # "anthropic" | "openai_compatible"
    base_url: str = ""
    env_key: str = ""
    env_base_url: str = ""
    needs_key: bool = True
    #: Fallback models, used only when the provider cannot be asked what it
    #: has. Every provider here is queried for its real model list first.
    default_generation_model: str = ""
    default_judge_model: str = ""
    local: bool = False
    hint: str = ""


BUILTIN: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic",
        kind="anthropic",
        env_key="ANTHROPIC_API_KEY",
        # The models autofab/agents.py itself uses: Sonnet to generate,
        # Opus to judge.
        default_generation_model="claude-sonnet-4-5-20250929",
        default_judge_model="claude-opus-4-20250514",
        hint="Set ANTHROPIC_API_KEY in .env",
    ),
    "openai": ProviderSpec(
        id="openai",
        label="OpenAI",
        kind="openai_compatible",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        hint="Set OPENAI_API_KEY in .env",
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="Ollama (local)",
        kind="openai_compatible",
        base_url="http://localhost:11434/v1",
        env_base_url="OLLAMA_BASE_URL",
        needs_key=False,
        local=True,
        hint="Start Ollama and pull a model, e.g. `ollama pull llama3.1`",
    ),
    "lmstudio": ProviderSpec(
        id="lmstudio",
        label="LM Studio (local)",
        kind="openai_compatible",
        base_url="http://localhost:1234/v1",
        env_base_url="LMSTUDIO_BASE_URL",
        needs_key=False,
        local=True,
        hint="Start LM Studio's local server",
    ),
    "custom": ProviderSpec(
        id="custom",
        label="Custom (OpenAI-compatible)",
        kind="openai_compatible",
        env_key="CADSMITH_LLM_API_KEY",
        env_base_url="CADSMITH_LLM_BASE_URL",
        needs_key=False,
        hint="Set CADSMITH_LLM_BASE_URL (and CADSMITH_LLM_API_KEY if needed) "
             "for vLLM, llama.cpp, Together, Groq, OpenRouter, and so on",
    ),
}

DEFAULT_PROVIDER = "anthropic"


# ---------------------------------------------------------------------------
# Session key overrides (memory only)
# ---------------------------------------------------------------------------

_session_keys: dict[str, str] = {}
_session_base_urls: dict[str, str] = {}
_keys_lock = threading.Lock()


def set_session_key(provider_id: str, api_key: str = "",
                    base_url: str = "") -> None:
    """Hold a key for this server process only. Never persisted or logged."""
    with _keys_lock:
        if api_key:
            _session_keys[provider_id] = api_key
        elif provider_id in _session_keys:
            del _session_keys[provider_id]
        if base_url:
            _session_base_urls[provider_id] = base_url
        elif provider_id in _session_base_urls:
            del _session_base_urls[provider_id]


def clear_session_keys() -> None:
    with _keys_lock:
        _session_keys.clear()
        _session_base_urls.clear()


def _api_key_for(spec: ProviderSpec) -> str:
    with _keys_lock:
        override = _session_keys.get(spec.id, "")
    return override or (os.getenv(spec.env_key, "") if spec.env_key else "")


def _base_url_for(spec: ProviderSpec) -> str:
    with _keys_lock:
        override = _session_base_urls.get(spec.id, "")
    env = os.getenv(spec.env_base_url, "") if spec.env_base_url else ""
    return (override or env or spec.base_url).rstrip("/")


# ---------------------------------------------------------------------------
# Resolved configuration
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    provider: str
    kind: str
    base_url: str
    api_key: str
    generation_model: str
    judge_model: str
    judge_vision: bool = True

    def redacted(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "generation_model": self.generation_model,
            "judge_model": self.judge_model,
            "judge_vision": self.judge_vision,
            "has_key": bool(self.api_key),
        }


def resolve(
    provider_id: str = DEFAULT_PROVIDER,
    generation_model: str = "",
    judge_model: str = "",
    judge_vision: bool = True,
) -> LLMConfig:
    spec = BUILTIN.get(provider_id) or BUILTIN[DEFAULT_PROVIDER]
    return LLMConfig(
        provider=spec.id,
        kind=spec.kind,
        base_url=_base_url_for(spec),
        api_key=_api_key_for(spec),
        generation_model=generation_model or spec.default_generation_model,
        judge_model=judge_model or spec.default_judge_model
                    or generation_model or spec.default_generation_model,
        judge_vision=judge_vision,
    )


def problems(config: LLMConfig) -> list[str]:
    """Reasons this configuration cannot run, in plain words."""
    spec = BUILTIN.get(config.provider)
    issues: list[str] = []
    if spec is None:
        return [f"Unknown provider '{config.provider}'."]
    if spec.needs_key and not config.api_key:
        issues.append(
            f"No API key for {spec.label}. {spec.hint}, or paste one in the app.")
    if spec.kind == "openai_compatible" and not config.base_url:
        issues.append(f"No base URL for {spec.label}. {spec.hint}.")
    if not config.generation_model:
        issues.append(f"No generation model chosen for {spec.label}.")
    if not config.judge_model:
        issues.append(f"No judge model chosen for {spec.label}.")
    return issues


def list_models(provider_id: str, timeout: float = 6.0) -> list[str]:
    """Ask the provider what it actually serves.

    Better than shipping a guessed list: a local Ollama reports the models
    that are really pulled, and a gateway reports what the key can reach.
    """
    spec = BUILTIN.get(provider_id)
    if spec is None:
        return []

    base_url = _base_url_for(spec)
    api_key = _api_key_for(spec)
    if not base_url and spec.kind == "anthropic":
        base_url = "https://api.anthropic.com/v1"

    headers = {}
    if spec.kind == "anthropic":
        if not api_key:
            return []
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif api_key:
        headers = {"Authorization": f"Bearer {api_key}"}

    try:
        response = httpx.get(f"{base_url}/models", headers=headers,
                             timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    entries = payload.get("data") or payload.get("models") or []
    names = [e.get("id") or e.get("name") for e in entries
             if isinstance(e, dict)]
    return sorted(n for n in names if n)


_reachable_cache: dict[str, tuple[float, bool]] = {}
_REACHABLE_TTL = 10.0


def _reachable(spec: ProviderSpec, base_url: str) -> bool:
    """Is a local server actually listening?

    A provider that needs no key would otherwise look ready whether or not
    Ollama is running, and the failure would surface only after the person
    hits Generate. The probe is cheap on localhost and briefly cached.
    """
    if not base_url:
        return False
    now = time.monotonic()
    cached = _reachable_cache.get(spec.id)
    if cached and now - cached[0] < _REACHABLE_TTL:
        return cached[1]

    try:
        response = httpx.get(f"{base_url}/models", timeout=1.5)
        ok = response.status_code < 500
    except Exception:
        ok = False
    _reachable_cache[spec.id] = (now, ok)
    return ok


def status() -> list[dict]:
    """What the app can offer right now, for the provider picker."""
    out = []
    for spec in BUILTIN.values():
        api_key = _api_key_for(spec)
        base_url = _base_url_for(spec)
        ready = (not spec.needs_key or bool(api_key)) and (
            spec.kind == "anthropic" or bool(base_url))
        # For a local server, having no key to check is not evidence of
        # anything - ask whether it is running.
        if ready and spec.local:
            ready = _reachable(spec, base_url)
        with _keys_lock:
            from_session = spec.id in _session_keys or spec.id in _session_base_urls
        out.append({
            "id": spec.id,
            "label": spec.label,
            "kind": spec.kind,
            "local": spec.local,
            "needs_key": spec.needs_key,
            "has_key": bool(api_key),
            "key_from_session": from_session,
            "base_url": base_url,
            "ready": ready,
            "hint": (f"Nothing is listening at {base_url}. {spec.hint}"
                     if spec.local and not ready else spec.hint),
            "default_generation_model": spec.default_generation_model,
            "default_judge_model": spec.default_judge_model,
        })
    return out


# ---------------------------------------------------------------------------
# The client shim
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)


_JSON_EXPECTED = "Output ONLY valid JSON"
_VISION_ERROR = re.compile(
    r"image|vision|multimodal|content.*not supported|unsupported.*content",
    re.IGNORECASE)


def _to_openai_messages(system: str, messages: list) -> list[dict]:
    """Translate Anthropic-shaped input into chat/completions form.

    The only structural difference that matters here is the image block: the
    Judge sends base64 PNGs, which OpenAI-compatible servers take as a data
    URL rather than a source object.
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message.get("role", "user"), "content": content})
            continue

        parts: list[dict] = []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif kind == "image":
                source = block.get("source") or {}
                media = source.get("media_type", "image/png")
                data = source.get("data", "")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{data}"},
                })
        out.append({"role": message.get("role", "user"), "content": parts})
    return out


def _strip_images(messages: list[dict]) -> tuple[list[dict], bool]:
    """Drop image parts, for a model that cannot accept them."""
    stripped = False
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        kept = [p for p in content if p.get("type") != "image_url"]
        if len(kept) != len(content):
            stripped = True
        out.append({**message, "content": kept or [{"type": "text", "text": ""}]})
    return out, stripped


def repair_json(text: str) -> str:
    """Recover a JSON object from a reply that wrapped it in prose.

    The Planner and the Judge both parse their replies strictly.  Frontier
    models honour "output only JSON"; smaller local ones often add a sentence
    before or after, which would otherwise abort the run.  Only the outermost
    balanced object is extracted, and only when the text does not already
    parse - a well-behaved reply is never touched.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        json.loads(candidate)
        return candidate
    except (json.JSONDecodeError, ValueError):
        pass

    start = candidate.find("{")
    if start == -1:
        return text
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                extracted = candidate[start:index + 1]
                try:
                    json.loads(extracted)
                    return extracted
                except (json.JSONDecodeError, ValueError):
                    return text
    return text


class OpenAICompatibleClient:
    """Presents the Anthropic client surface over /chat/completions."""

    def __init__(self, config: LLMConfig, on_note=None):
        self.config = config
        self._on_note = on_note

    # -- surface ------------------------------------------------------------

    @property
    def messages(self) -> "OpenAICompatibleClient":
        return self

    def create(self, *, model: str = "", max_tokens: int = 4096,
               system: str = "", messages: Optional[list] = None,
               **_: Any) -> _Response:
        role = self._role_for(system)
        target = (self.config.judge_model if role == "judge"
                  else self.config.generation_model)

        payload_messages = _to_openai_messages(system, messages or [])
        if role == "judge" and not self.config.judge_vision:
            payload_messages, _ = _strip_images(payload_messages)

        try:
            text, usage = self._post(target, payload_messages, max_tokens)
        except _VisionUnsupported:
            payload_messages, stripped = _strip_images(payload_messages)
            if stripped:
                self._note(
                    f"{target} rejected the rendered image; the Judge is "
                    f"running on kernel metrics alone.")
            text, usage = self._post(target, payload_messages, max_tokens)

        if _JSON_EXPECTED in system:
            text = repair_json(text)

        return _Response(content=[_Block(text=text)], usage=usage)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _role_for(system: str) -> str:
        """Judge or generation, decided by the agent's own system prompt.

        Keyed on the prompt rather than the model id hardcoded at the call
        site, so the two roles stay distinguishable however they are
        configured.
        """
        try:
            from autofab import agents

            if system and system == agents.VALIDATOR_SYSTEM:
                return "judge"
        except Exception:
            pass
        return "judge" if system.startswith("You are the Validator Agent") \
            else "generation"

    def _post(self, model: str, messages: list[dict],
              max_tokens: int) -> tuple[str, _Usage]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }

        try:
            response = httpx.post(
                f"{self.config.base_url}/chat/completions",
                headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Could not reach {self.config.provider} at "
                f"{self.config.base_url}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:600]
            if response.status_code == 400 and _VISION_ERROR.search(detail):
                raise _VisionUnsupported(detail)
            raise RuntimeError(
                f"{self.config.provider} returned {response.status_code} for "
                f"model '{model}': {detail}")

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(
                f"{self.config.provider} returned no completion: "
                f"{json.dumps(payload)[:400]}")

        text = (choices[0].get("message") or {}).get("content") or ""
        raw_usage = payload.get("usage") or {}
        usage = _Usage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
        )
        return text, usage

    def _note(self, message: str) -> None:
        if self._on_note:
            try:
                self._on_note(message)
            except Exception:
                pass


class _VisionUnsupported(RuntimeError):
    """The model refused an image; retry without one."""


def build_client(config: LLMConfig, on_note=None):
    """Return a client for this configuration.

    For Anthropic that is the real SDK object, so the default path behaves
    exactly as the published pipeline does.
    """
    if config.kind == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=config.api_key)
    return OpenAICompatibleClient(config, on_note=on_note)

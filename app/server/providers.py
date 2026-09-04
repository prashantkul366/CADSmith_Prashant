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

#: Status codes that mean "the request never reached a model, try again".
#: 524 is Cloudflare's origin-timeout page, which a free trycloudflare.com
#: tunnel serves after about 100 seconds even though the model is still
#: working. Anything not listed here is a real answer and is not retried.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

#: One agent call is worth a couple of extra attempts; more than that just
#: makes a broken endpoint take longer to report.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (2.0, 5.0)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str                     # "anthropic" | "openai_compatible" | "bedrock"
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
    #: Bedrock only: where the AWS region comes from when none is chosen.
    env_region: str = ""


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
    "bedrock": ProviderSpec(
        id="bedrock",
        label="AWS Bedrock (Anthropic)",
        kind="bedrock",
        # Bedrock authenticates with AWS credentials, not an Anthropic key -
        # env vars, a named profile, an SSO session or an instance role, all
        # resolved by botocore. There is nothing to paste into a key field.
        needs_key=False,
        env_region="AWS_REGION",
        # Same split the pipeline uses everywhere: a mid-tier model writes
        # the CadQuery, a stronger independent one judges it. Bedrock model
        # ids carry an "anthropic." prefix.
        default_generation_model="anthropic.claude-sonnet-5",
        default_judge_model="anthropic.claude-opus-5",
        hint="Set AWS_REGION and your usual AWS credentials "
             "(AWS_PROFILE, env vars, SSO or an instance role)",
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
#: Bedrock: region and profile *name* chosen in the app. Neither is a secret -
#: the credentials themselves stay with botocore and never reach this process.
_session_regions: dict[str, str] = {}
_session_profiles: dict[str, str] = {}
_keys_lock = threading.Lock()


def set_session_aws(provider_id: str, region: str = "",
                    profile: str = "") -> None:
    """Choose a Bedrock region and profile for this server process.

    Deliberately separate from ``set_session_key``: Bedrock has no key to
    hold, and treating a region like a secret would be misleading in both
    directions - it is safe to display, and it is not what authenticates you.
    """
    with _keys_lock:
        for store, value in ((_session_regions, region),
                             (_session_profiles, profile)):
            if value:
                store[provider_id] = value
            elif provider_id in store:
                del store[provider_id]


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


def _profile_for(spec: ProviderSpec) -> str:
    if spec.kind != "bedrock":
        return ""
    with _keys_lock:
        override = _session_profiles.get(spec.id, "")
    return override or os.getenv("AWS_PROFILE", "")


def clear_session_keys() -> None:
    with _keys_lock:
        _session_keys.clear()
        _session_base_urls.clear()


def _api_key_for(spec: ProviderSpec) -> str:
    with _keys_lock:
        override = _session_keys.get(spec.id, "")
    return override or (os.getenv(spec.env_key, "") if spec.env_key else "")


def _region_for(spec: ProviderSpec) -> str:
    """The AWS region for Bedrock, in the order AWS tooling itself uses.

    A pasted region is held for this process like a session key; otherwise
    AWS_REGION, then AWS_DEFAULT_REGION, then whatever the active profile
    configures. Bedrock will not accept a request without one.
    """
    if spec.kind != "bedrock":
        return ""
    with _keys_lock:
        override = _session_regions.get(spec.id, "")
    if override:
        return override
    for name in (spec.env_region, "AWS_DEFAULT_REGION"):
        if name and os.getenv(name):
            return os.getenv(name, "")
    try:
        import botocore.session

        return botocore.session.get_session().get_config_variable("region") or ""
    except Exception:
        return ""


_aws_probe_cache: tuple[float, tuple[bool, str]] | None = None
_AWS_PROBE_TTL = 30.0


def _aws_credentials_found(force: bool = False) -> tuple[bool, str]:
    """Whether botocore can resolve credentials, and where from.

    Reported rather than assumed: "no ANTHROPIC_API_KEY" is a clear failure,
    but AWS credentials come from six places and silently having none is the
    usual way a Bedrock demo dies.

    Cached for a few seconds, and the instance-metadata leg of the chain is
    given a short timeout. The health endpoint calls this on every request,
    and on a host where IMDS is firewalled rather than absent the default
    chain waits seconds each time.
    """
    global _aws_probe_cache
    now = time.time()
    if not force and _aws_probe_cache is not None:
        stamped, value = _aws_probe_cache
        if now - stamped < _AWS_PROBE_TTL:
            return value

    result: tuple[bool, str]
    try:
        import botocore.session
        from botocore.config import Config
    except ImportError:
        result = (False,
                  "boto3 is not installed - pip install 'anthropic[bedrock]'")
    else:
        try:
            session = botocore.session.get_session()
            session.set_default_client_config(
                Config(connect_timeout=2, read_timeout=2,
                       retries={"max_attempts": 1}))
            credentials = session.get_credentials()
            result = ((False, "no AWS credentials found") if credentials is None
                      else (True, getattr(credentials, "method", "aws") or "aws"))
        except Exception as error:                   # pragma: no cover
            result = (False, f"{type(error).__name__}: {error}")

    _aws_probe_cache = (now, result)
    return result


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
    #: Bedrock only. Never a secret - the region and profile *name* are safe
    #: to show; the credentials themselves are resolved by botocore and never
    #: pass through this process.
    aws_region: str = ""
    aws_profile: str = ""

    def redacted(self) -> dict:
        out = {
            "provider": self.provider,
            "base_url": self.base_url,
            "generation_model": self.generation_model,
            "judge_model": self.judge_model,
            "judge_vision": self.judge_vision,
            "has_key": bool(self.api_key),
        }
        if self.kind == "bedrock":
            out["aws_region"] = self.aws_region
            out["aws_profile"] = self.aws_profile
        return out


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
        aws_region=_region_for(spec),
        aws_profile=_profile_for(spec),
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
    if spec.kind == "bedrock":
        if not config.aws_region:
            issues.append(
                "No AWS region for Bedrock. Set AWS_REGION in .env, or "
                "choose one in the app - Bedrock will not accept a request "
                "without a region.")
        found, detail = _aws_credentials_found()
        if not found:
            issues.append(
                f"Bedrock cannot authenticate: {detail}. It uses AWS "
                f"credentials, not an Anthropic API key - configure "
                f"AWS_PROFILE, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, an "
                f"SSO session, or an instance role.")
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
    if spec.kind == "bedrock":
        return _list_bedrock_models(spec, timeout=timeout)

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


def _list_bedrock_models(spec: ProviderSpec, timeout: float = 6.0) -> list[str]:
    """What this AWS account can actually invoke, asked of Bedrock itself.

    ListFoundationModels needs the ``bedrock:ListFoundationModels`` permission,
    which is separate from ``bedrock:InvokeModel`` - a role that can run the
    pipeline perfectly well may not be allowed to enumerate. So a failure here
    is not an error: fall back to the two defaults rather than presenting an
    empty picker as if nothing were available.
    """
    region = _region_for(spec)
    if not region:
        return []
    try:
        import boto3
        from botocore.config import Config

        session_kwargs = {}
        profile = _profile_for(spec)
        if profile:
            session_kwargs["profile_name"] = profile
        session = boto3.Session(**session_kwargs)
        client = session.client(
            "bedrock", region_name=region,
            config=Config(connect_timeout=timeout, read_timeout=timeout,
                          retries={"max_attempts": 1}))
        payload = client.list_foundation_models(byProvider="anthropic")
    except Exception:
        return [spec.default_generation_model, spec.default_judge_model]

    names = {entry.get("modelId") for entry in payload.get("modelSummaries", [])
             if entry.get("modelId")}
    # Bedrock also exposes cross-region inference profiles ("us.anthropic...")
    # which are what many accounts are actually entitled to invoke. Keep both.
    return sorted(n for n in names if "anthropic" in n) or [
        spec.default_generation_model, spec.default_judge_model]


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
            spec.kind in ("anthropic", "bedrock") or bool(base_url))
        # Needing no key is not evidence of anything for Bedrock either: it
        # is ready only when a region and real AWS credentials both resolve.
        if spec.kind == "bedrock":
            ready = bool(_region_for(spec)) and _aws_credentials_found()[0]
        # For a local server, having no key to check is not evidence of
        # anything - ask whether it is running.
        if ready and spec.local:
            ready = _reachable(spec, base_url)
        with _keys_lock:
            from_session = (spec.id in _session_keys
                            or spec.id in _session_base_urls
                            or spec.id in _session_regions
                            or spec.id in _session_profiles)
        entry = {
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
            # The same sentence as a key the browser can look up in whatever
            # language it is showing. `hint` stays as it is: it is also used
            # in the server's own refusal messages, and it is the fallback
            # for anything the browser dictionary has not got.
            "hint_key": ("prov.hint.unreachable" if spec.local and not ready
                         else f"prov.hint.{spec.id}"),
            "hint_params": {"url": base_url, "id": spec.id},
            "default_generation_model": spec.default_generation_model,
            "default_judge_model": spec.default_judge_model,
        }
        if spec.kind == "bedrock":
            found, source = _aws_credentials_found()
            entry["aws_region"] = _region_for(spec)
            entry["aws_profile"] = _profile_for(spec)
            entry["aws_credentials"] = source if found else ""
            if not ready:
                entry["hint"] = (
                    "Bedrock needs an AWS region"
                    if not entry["aws_region"] else
                    f"AWS credentials not found ({source}). {spec.hint}")
                entry["hint_key"] = ("prov.hint.bedrock.noregion"
                                     if not entry["aws_region"]
                                     else "prov.hint.bedrock.nocreds")
                entry["hint_params"] = {"source": source}
        out.append(entry)
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


def _role_for(system: str) -> str:
    """Judge or generation, decided by the agent's own system prompt.

    Keyed on the prompt rather than the model id hardcoded at the call site,
    so the two roles stay distinguishable however they are configured.
    """
    try:
        from autofab import agents

        if system and system == agents.VALIDATOR_SYSTEM:
            return "judge"
    except Exception:
        pass
    return "judge" if system.startswith("You are the Validator Agent") \
        else "generation"


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
        return _role_for(system)

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

        response = self._post_with_retry(headers, body, model)
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(
                f"{self.config.provider} returned no completion: "
                f"{json.dumps(payload)[:400]}")

        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not text.strip() and message.get("reasoning"):
            # Some models put the answer in a "reasoning" field and leave
            # content empty. Say so, rather than letting the agent fail later
            # on an unparseable empty string.
            raise RuntimeError(
                f"{self.config.provider} model '{model}' replied with "
                f"reasoning only and no content. The pipeline reads the "
                f"content field, so this model cannot be used for that role.")
        raw_usage = payload.get("usage") or {}
        usage = _Usage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
        )
        return text, usage

    def _post_with_retry(self, headers: dict, body: dict,
                         model: str) -> httpx.Response:
        """POST once, retrying only failures that never reached the model.

        A gateway timeout or a dropped connection costs a whole refinement
        iteration if it is allowed through, so it is worth a second look.
        A 400 or a 401 is the endpoint's real answer and is raised at once.
        """
        url = f"{self.config.base_url}/chat/completions"
        last = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = httpx.post(url, headers=headers, json=body,
                                      timeout=REQUEST_TIMEOUT)
            except httpx.TimeoutException as exc:
                # The full timeout has already been spent waiting; spending
                # it again is worse than reporting it.
                raise RuntimeError(
                    f"{self.config.provider} at {self.config.base_url} did "
                    f"not answer within {REQUEST_TIMEOUT:.0f}s. Raise "
                    f"CADSMITH_LLM_TIMEOUT if the model is simply slow."
                ) from exc
            except httpx.HTTPError as exc:
                last = (f"Could not reach {self.config.provider} at "
                        f"{self.config.base_url}: {exc}")
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(last) from exc
            else:
                if response.status_code < 400:
                    return response

                detail = response.text[:600]
                if (response.status_code == 400
                        and _VISION_ERROR.search(detail)):
                    raise _VisionUnsupported(detail)

                last = (f"{self.config.provider} returned "
                        f"{response.status_code} for model '{model}': "
                        f"{_explain_status(response.status_code, detail)}")
                if (response.status_code not in TRANSIENT_STATUS
                        or attempt == MAX_ATTEMPTS):
                    raise RuntimeError(last)

            self._note(
                f"{self.config.provider} attempt {attempt} of "
                f"{MAX_ATTEMPTS} failed, retrying: {last[:160]}")
            time.sleep(RETRY_BACKOFF[min(attempt - 1,
                                         len(RETRY_BACKOFF) - 1)])

        raise RuntimeError(last)   # pragma: no cover - loop always returns

    def _note(self, message: str) -> None:
        if self._on_note:
            try:
                self._on_note(message)
            except Exception:
                pass


def _explain_status(status: int, detail: str) -> str:
    """Turn a gateway error page into one sentence a person can act on.

    A tunnel or proxy answers with HTML, not JSON, so the raw body is a
    screenful of markup that says nothing about the model. Recognise the
    ones that have a known cause and say what to do about them.
    """
    body = detail.strip()
    looks_like_html = body[:200].lower().lstrip().startswith(
        ("<!doctype", "<html"))

    if status in (524, 522):
        return ("the tunnel in front of the model timed out waiting for it. "
                "A free trycloudflare.com tunnel gives up after about 100 "
                "seconds however long the app is willing to wait, so a slow "
                "reply is cut off even though the model is still working. "
                "Use a tunnel without that limit, or lower max_tokens.")
    if status == 429:
        return "the endpoint is rate limiting this key."
    if status in (502, 503):
        return ("the endpoint is up but the model behind it is not "
                "answering. It may still be loading.")
    if looks_like_html:
        return (f"the endpoint answered with an HTML error page rather than "
                f"JSON, so something in front of the model handled the "
                f"request. First line: {' '.join(body.split())[:120]}")
    return body


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
    if config.kind == "bedrock":
        return _bedrock_client(config, on_note=on_note)
    return OpenAICompatibleClient(config, on_note=on_note)


class BedrockClient:
    """The Bedrock SDK client, with the model id chosen by role.

    ``autofab.agents`` hardcodes its model ids at the call site
    (``claude-sonnet-4-5-20250929`` to generate, ``claude-opus-4-20250514``
    to judge). Those are Anthropic-API ids; Bedrock wants its own, prefixed
    ``anthropic.``. Handing the SDK client straight to the agents would send
    an id Bedrock has never heard of, and the run would die on the first call
    with a validation error naming a model nobody chose.

    So this substitutes the configured model by role, exactly as the
    OpenAI-compatible shim does - but it is a much thinner thing, because
    Bedrock speaks the Messages API natively. Nothing about the request shape
    is translated; only the model id is replaced.
    """

    def __init__(self, inner, config: LLMConfig, on_note=None):
        self._inner = inner
        self.config = config
        self._on_note = on_note

    @property
    def messages(self) -> "BedrockClient":
        return self

    def create(self, *, model: str = "", system: str = "",
               messages: Optional[list] = None, **kwargs: Any):
        role = _role_for(system)
        target = (self.config.judge_model if role == "judge"
                  else self.config.generation_model)

        payload = list(messages or [])
        if role == "judge" and not self.config.judge_vision:
            payload, _ = _strip_images(payload)

        return self._inner.messages.create(
            model=target, system=system, messages=payload, **kwargs)

    def __getattr__(self, name):        # anything else, straight through
        return getattr(self._inner, name)


def _bedrock_client(config: LLMConfig, on_note=None):
    """An Anthropic client backed by AWS Bedrock.

    The Mantle client speaks the Messages API, so it exposes the same
    ``messages.create`` surface as the first-party SDK and the whole pipeline
    works through it unchanged - no shim, unlike the OpenAI-compatible path.

    Credentials are never passed in: botocore resolves them from the profile,
    environment, SSO session or instance role, which is what makes an IAM
    role work at all. Only the region and the profile *name* come from here.
    """
    import anthropic

    kwargs: dict = {"aws_region": config.aws_region}
    if config.aws_profile:
        kwargs["aws_profile"] = config.aws_profile

    # The Mantle endpoint is the current path. Some accounts and regions are
    # still on the older bedrock-runtime InvokeModel route, so allow falling
    # back rather than leaving someone stuck.
    if os.getenv("CADSMITH_BEDROCK_LEGACY", "").strip().lower() in ("1", "true", "yes"):
        if on_note:
            on_note("Bedrock: using the legacy bedrock-runtime InvokeModel path "
                    "(CADSMITH_BEDROCK_LEGACY is set).")
        return BedrockClient(anthropic.AnthropicBedrock(**kwargs), config,
                             on_note=on_note)
    return BedrockClient(anthropic.AnthropicBedrockMantle(**kwargs), config,
                         on_note=on_note)

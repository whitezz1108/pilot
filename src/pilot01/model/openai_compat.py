"""The one live provider adapter: OpenAI-compatible chat completions.

Isolated here on purpose. It is the only module in the repository that knows
about HTTP, URLs, auth headers or a response envelope, and it is reached only
through :class:`~pilot01.model.client.ModelClient`. An agent node never imports
it.

Design choices, all in service of "a test must never touch the network":

* **The transport is injectable.** ``transport`` takes the prepared
  ``urllib.request.Request`` and returns the response body. The default uses
  :mod:`urllib.request` from the standard library -- no new dependency -- and
  the tests substitute a fake, so the request that *would* be sent can be
  inspected without sending it.
* **The credential lives on this object and nowhere else.** It is read from the
  environment at construction, put into one header, and never copied into a
  :class:`~pilot01.model.client.ModelRequest` or
  :class:`~pilot01.model.client.ModelResponse`. If a provider ever echoes it
  back, :func:`pilot01.model.log.assert_no_secrets` refuses to log the record.
* **Nothing constructs this implicitly.** There is no default client, no
  registry entry that falls back to it, and no code path that reads an API key
  unless a caller asked for a live client by name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from .reasoning import StreamCapture, count_local_tokens, observe_reasoning
from .tool_protocol import ModelToolCall, ToolTransport

from .client import (
    ModelClientError,
    ModelParams,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)

__all__ = [
    "OpenAICompatibleClient",
    "Transport",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL_ENV",
    "missing_credentials",
]

DEFAULT_API_KEY_ENV = "PILOT01_API_KEY"
DEFAULT_BASE_URL_ENV = "PILOT01_BASE_URL"

Transport = Callable[[urllib.request.Request, float], bytes]
"""``(prepared_request, timeout_seconds) -> response_body_bytes``."""


def missing_credentials() -> tuple[str, ...]:
    """Which of the two required environment variables are unset, **by name**.

    Exists so that a caller can fail before the first paid call without being
    told the credential's name. This module is the only one in the repository
    that knows it -- a test enforces that -- and a caller that needs to check
    for the key asks here rather than reading the environment itself.

    Returns names only. The values are never read into this function's result,
    and never returned to a caller that could print one.
    """
    missing = []
    if not os.environ.get(DEFAULT_BASE_URL_ENV):
        missing.append(DEFAULT_BASE_URL_ENV)
    if not os.environ.get(DEFAULT_API_KEY_ENV):
        missing.append(DEFAULT_API_KEY_ENV)
    return tuple(missing)


def _urlopen_transport(request: urllib.request.Request, timeout: float) -> bytes:
    """The real transport. Standard library only; the sole network call site."""
    request.pilot01_stream_capture = None
    capture = StreamCapture() if json.loads(request.data).get("stream") else None
    with urllib.request.urlopen(request, timeout=timeout) as handle:  # noqa: S310
        if capture is not None:
            request.pilot01_stream_capture = capture
            # Parse complete SSE events, not arbitrary TCP chunks. Keep partial
            # evidence on the request if the stream fails; never retry it.
            data = []
            for raw_line in handle:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        capture.feed("\n".join(data))
                        data = []
                        if capture.done:
                            break
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            if data:
                capture.feed("\n".join(data))
            if not capture.done or capture.envelope["choices"][0]["finish_reason"] is None:
                raise ModelClientError("incomplete model SSE stream")
            return json.dumps(capture.snapshot()).encode("utf-8")
        return handle.read()


def _redact(text: str, secret: str | None) -> str:
    """Remove a credential from anything that might be shown or logged."""
    if secret:
        text = text.replace(secret, "***REDACTED***")
    return text


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


def read_sglang_capacity(base_url):
    """Read local serving metadata inside the existing provider network boundary."""
    endpoint = base_url.rstrip("/").removesuffix("/v1") + "/get_server_info"
    try:
        with urllib.request.urlopen(endpoint, timeout=15) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        raise ModelClientError(f"serving_capacity_failure: cannot read actual SGLang capacity ({type(exc).__name__})") from exc


class OpenAICompatibleClient:
    """A :class:`~pilot01.model.client.ModelClient` for OpenAI-shaped endpoints.

    Constructing one is the opt-in to live calls. Nothing in this repository
    does so implicitly.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        provider: str = "openai-compatible",
        json_mode: bool = True,
        transport: Transport | None = None,
        extra_headers: Mapping[str, str] | None = None,
        reasoning_stream: bool = False,
        reasoning_tokenizer_path: str | None = None,
        tool_transport: ToolTransport = "legacy-text",
        context_window_tokens: int | None = None,
        context_safety_tokens: int = 0,
        minimum_output_tokens: int = 1,
    ) -> None:
        resolved_url = base_url or os.environ.get(DEFAULT_BASE_URL_ENV)
        if not resolved_url:
            raise ModelClientError(
                f"no base URL for the model endpoint; pass base_url= or set "
                f"{DEFAULT_BASE_URL_ENV}"
            )
        resolved_key = api_key if api_key is not None else os.environ.get(api_key_env)
        if not resolved_key:
            raise ModelClientError(
                f"no API key for the model endpoint; pass api_key= or set "
                f"{api_key_env}. Keys are read from the environment and are "
                "never written to a config file, a request record, or the log."
            )

        self._base_url = resolved_url.rstrip("/")
        self._api_key = resolved_key
        self._provider = provider
        self._json_mode = json_mode
        # Explicit opt-in for the local experiment; other providers retain JSON mode.
        grammar_path = os.environ.get("PILOT01_JSON_OBJECT_GRAMMAR_PATH")
        self._json_object_grammar = None
        if json_mode and grammar_path:
            try:
                self._json_object_grammar = Path(grammar_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ModelClientError("cannot read configured JSON object grammar") from exc
            if not self._json_object_grammar.strip():
                raise ModelClientError("configured JSON object grammar is empty")
        self._transport: Transport = transport or _urlopen_transport
        self._extra_headers = dict(extra_headers or {})
        self._reasoning_stream = reasoning_stream
        self._reasoning_tokenizer_path = reasoning_tokenizer_path
        self._tool_transport = tool_transport
        if context_window_tokens is not None and context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if context_safety_tokens < 0 or minimum_output_tokens < 1:
            raise ValueError("context safety must be nonnegative and minimum output positive")
        if context_window_tokens is not None and not reasoning_tokenizer_path:
            raise ValueError("context budgeting requires reasoning_tokenizer_path")
        self._context_window_tokens = context_window_tokens
        self._context_safety_tokens = context_safety_tokens
        self._minimum_output_tokens = minimum_output_tokens

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    @property
    def tool_transport(self) -> ToolTransport:
        return self._tool_transport

    def prepare_request(self, request: ModelRequest) -> ModelRequest:
        """Fit the declared completion into the endpoint before any HTTP call."""
        if self._context_window_tokens is None:
            return request
        try:
            estimated_input, tokenizer_sha = self._estimate_request(request)
        except Exception as exc:
            raise ModelClientError(
                f"context_budget_failure: local tokenizer unavailable ({type(exc).__name__})"
            ) from exc
        available = (self._context_window_tokens - self._context_safety_tokens
                     - estimated_input)
        if available < self._minimum_output_tokens:
            raise ModelClientError(
                "context_budget_failure: estimated input "
                f"{estimated_input} + safety {self._context_safety_tokens} leaves "
                f"{available} output tokens in context {self._context_window_tokens}; "
                f"minimum is {self._minimum_output_tokens}; tokenizer={tokenizer_sha[:12]}"
            )
        effective = min(request.params.max_output_tokens, available)
        if effective == request.params.max_output_tokens:
            return request
        params = request.params.model_copy(update={"max_output_tokens": effective})
        return request.model_copy(update={"params": params})

    def _estimate_request(self, request: ModelRequest) -> tuple[int, str]:
        # Count only provider-visible conversational material. The fixed safety
        # reserve covers chat-template framing and tokenizer/server differences.
        material = {
            "messages": [message.wire_dict() for message in request.messages],
            "tools": [tool.openai_function() for tool in request.tools],
        }
        serialized = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
        return count_local_tokens(serialized, self._reasoning_tokenizer_path)

    def estimate_request_tokens(self, request: ModelRequest) -> int:
        """Estimate the provider-visible input without sending an HTTP request."""
        if not self._reasoning_tokenizer_path:
            raise ModelClientError("request token estimation requires a local tokenizer")
        try:
            count, _ = self._estimate_request(request)
        except Exception as exc:
            raise ModelClientError(
                f"context_budget_failure: local tokenizer unavailable ({type(exc).__name__})"
            ) from exc
        return count

    # -- request construction (pure; exercised by tests without a network) ---

    def build_request(self, request: ModelRequest) -> urllib.request.Request:
        """Turn a :class:`ModelRequest` into an HTTP request.

        Kept separate from :meth:`generate` so the exact wire payload can be
        asserted in a test with no transport at all.
        """
        params: ModelParams = request.params
        payload: dict[str, object] = {
            "model": params.model_id,
            "messages": [message.wire_dict() for message in request.messages],
            "temperature": params.temperature,
        }
        if params.max_output_tokens is not None:
            payload["max_tokens"] = params.max_output_tokens
        if params.reasoning_effort is not None:
            payload["reasoning_effort"] = params.reasoning_effort
        if params.top_p is not None:
            payload["top_p"] = params.top_p
        for name in (
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "seed",
        ):
            value = getattr(params, name)
            if value is not None:
                payload[name] = value
        strict_tool_protocol = os.environ.get("PILOT01_STRICT_TOOL_PROTOCOL") == "1"
        if request.tools:
            if self._tool_transport != "native":
                raise ModelClientError(
                    "request declares native tools but this client is configured for legacy-text tool transport"
                )
            if strict_tool_protocol:
                raise ModelClientError(
                    "native tool calls cannot be combined with the legacy PILOT01_STRICT_TOOL_PROTOCOL grammar"
                )
            payload["tools"] = [tool.openai_function() for tool in request.tools]
            payload["tool_choice"] = request.tool_choice or "auto"
            # v1 native experiments permitted batches.  The strict v2 state
            # machine sets this false per request; retain v1 when unspecified.
            payload["parallel_tool_calls"] = (
                True if request.parallel_tool_calls is None else request.parallel_tool_calls
            )
        elif self._json_mode:
            if self._json_object_grammar is not None:
                payload["ebnf"] = self._json_object_grammar
            else:
                payload["response_format"] = {"type": "json_object"}
        if self._reasoning_stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        if strict_tool_protocol:
            from .protocol_constraints import build_schema, compile_grammar
            payload.pop('response_format', None)
            payload['ebnf'] = compile_grammar(build_schema(payload['messages'], request.role))

        if request.native_output_format is not None:
            if self._tool_transport != "native" or strict_tool_protocol:
                raise ModelClientError("closed native output requires native transport without the legacy grammar")
            from .native_constraints import closed_native_response_format
            payload.pop("ebnf", None)
            payload["response_format"] = closed_native_response_format(request.final_output_schema, request.tools)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        headers.update(self._extra_headers)
        return urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    # -- ModelClient --------------------------------------------------------

    def generate(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise ModelClientError(
                f"openai-compatible client received {type(request).__name__}; "
                "expected ModelRequest"
            )
        request = self.prepare_request(request)
        http_request = self.build_request(request)
        # Local transport metadata only; never a header or a provider parameter.
        http_request.pilot01_model_call_id = request.model_call_id
        requested_at = datetime.now(timezone.utc)

        try:
            body = self._transport(http_request, request.params.timeout_seconds)
        except urllib.error.HTTPError as exc:
            detail = _truncate(_redact(_safe_read(exc), self._api_key))
            raise ModelClientError(
                f"model endpoint returned HTTP {exc.code} for "
                f"{self.endpoint}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            error = ModelClientError(
                f"could not reach the model endpoint {self.endpoint}: "
                f"{_redact(str(exc.reason), self._api_key)}"
            )
            self._attach_partial(error, http_request, request, requested_at)
            raise error from exc
        except TimeoutError as exc:
            error = ModelClientError(
                f"model endpoint {self.endpoint} timed out after "
                f"{request.params.timeout_seconds}s"
            )
            self._attach_partial(error, http_request, request, requested_at)
            raise error from exc
        except (ModelClientError, ValueError, OSError) as exc:
            detail = _truncate(_redact(str(exc), self._api_key))
            error = ModelClientError(f"model stream failed: {type(exc).__name__}: {detail}")
            self._attach_partial(error, http_request, request, requested_at)
            raise error from exc

        responded_at = datetime.now(timezone.utc)
        envelope = self._decode(body)
        try:
            return self._response(envelope, http_request, request, requested_at, responded_at)
        except ModelClientError as exc:
            exc.partial_response = self._partial_response_from_envelope(
                envelope,
                http_request,
                request,
                requested_at,
                responded_at,
            )
            raise

    def _attach_partial(self, error, http_request, request, requested_at):
        capture = getattr(http_request, "pilot01_stream_capture", None)
        if capture is not None:
            error.partial_response = self._partial_response_from_envelope(
                capture.snapshot(),
                http_request,
                request,
                requested_at,
                datetime.now(timezone.utc),
            )

    def _partial_response_from_envelope(
        self,
        envelope,
        http_request,
        request,
        requested_at,
        responded_at,
    ):
        """Preserve a malformed provider envelope without normalizing its calls."""
        capture = getattr(http_request, "pilot01_stream_capture", None)
        redacted = json.loads(_redact(json.dumps(envelope), self._api_key))
        reasoning, observation = observe_reasoning(
            redacted,
            timing=capture.timing() if capture is not None else None,
            tokenizer_path=self._reasoning_tokenizer_path,
        )
        provider_response = dict(redacted)
        if capture is not None:
            provider_response["stream_events"] = json.loads(
                _redact(json.dumps(capture.events), self._api_key)
            )
        try:
            text = self._extract_text(redacted)
        except ModelClientError:
            text = ""
        return ModelResponse(
            text=text,
            provider=self._provider,
            model_id=str(redacted.get("model") or request.params.model_id),
            requested_at=requested_at,
            responded_at=responded_at,
            request_id=redacted.get("id"),
            usage=self._extract_usage(redacted),
            finish_reason=self._extract_finish_reason(redacted),
            reasoning_content=reasoning,
            reasoning_observation=observation,
            provider_response=provider_response,
            tool_calls=(),
        )

    def _response(self, envelope, http_request, request, requested_at, responded_at):
        capture = getattr(http_request, "pilot01_stream_capture", None)
        text = self._extract_text(envelope)
        # Remove an echoed credential before any newly retained provider payload
        # reaches the log. The log's existing secret scanner still applies.
        envelope = json.loads(_redact(json.dumps(envelope), self._api_key))
        reasoning, observation = observe_reasoning(envelope,
            timing=capture.timing() if capture is not None else None,
            tokenizer_path=self._reasoning_tokenizer_path)
        provider_response = dict(envelope)
        if capture is not None:
            provider_response["stream_events"] = json.loads(_redact(json.dumps(capture.events), self._api_key))

        return ModelResponse(
            text=text,
            provider=self._provider,
            model_id=str(envelope.get("model") or request.params.model_id),
            requested_at=requested_at,
            responded_at=responded_at,
            request_id=envelope.get("id"),
            usage=self._extract_usage(envelope),
            finish_reason=self._extract_finish_reason(envelope),
            reasoning_content=reasoning,
            reasoning_observation=observation,
            provider_response=provider_response,
            tool_calls=self._extract_tool_calls(
                envelope,
                model_call_id=request.model_call_id,
            ),
        )

    # -- response decoding --------------------------------------------------

    def _decode(self, body: bytes) -> dict:
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelClientError(
                f"model endpoint returned a body that is not JSON: "
                f"{_truncate(_redact(body.decode('utf-8', 'replace'), self._api_key))}"
            ) from exc
        if not isinstance(envelope, dict):
            raise ModelClientError(
                f"model endpoint returned a JSON {type(envelope).__name__}; expected an object"
            )
        if "error" in envelope and "choices" not in envelope:
            raise ModelClientError(
                f"model endpoint returned an error: "
                f"{_truncate(_redact(json.dumps(envelope['error']), self._api_key))}"
            )
        return envelope

    @staticmethod
    def _extract_text(envelope: dict) -> str:
        choices = envelope.get("choices") or []
        if not choices:
            raise ModelClientError(
                "model endpoint returned no choices: "
                f"{_truncate(json.dumps({k: v for k, v in envelope.items() if k != 'usage'}))}"
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None and message.get("tool_calls"):
            return ""
        if content is None and isinstance(message.get("reasoning_content"), str):
            return ""  # Preserve reasoning-only/truncated responses for auditing.
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Some OpenAI-compatible endpoints return content parts.
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        raise ModelClientError(
            f"model endpoint returned a message with no usable content "
            f"(content is {type(content).__name__})"
        )

    @staticmethod
    def _extract_tool_calls(
        envelope: dict,
        *,
        model_call_id: str,
    ) -> tuple[ModelToolCall, ...]:
        choices = envelope.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ()
        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelClientError("model endpoint returned tool_calls that is not a list")
        calls = []
        for index, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                raise ModelClientError(f"tool_calls[{index}] is not an object")
            function = raw.get("function") or {}
            call_id = raw.get("id")
            call_id_origin = "provider"
            name = function.get("name") if isinstance(function, dict) else None
            raw_arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(call_id, str) or not call_id:
                call_id = f"call_adapter_{model_call_id}_{index}"
                call_id_origin = "adapter_generated"
            if not isinstance(name, str) or not name:
                raise ModelClientError(f"tool_calls[{index}] has no function name")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    arguments = raw_arguments
            else:
                arguments = raw_arguments
            calls.append(
                ModelToolCall(
                    call_id=call_id,
                    call_id_origin=call_id_origin,
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_arguments if isinstance(raw_arguments, str) else None,
                )
            )
        return tuple(calls)

    @staticmethod
    def _extract_finish_reason(envelope: dict) -> str | None:
        """Why the provider stopped, or ``None`` if it does not say.

        Read rather than judged. ``"length"`` is reported to the caller as
        ``"length"`` and not converted into an error here, because an adapter
        that decided what a truncation meant would be deciding something that
        belongs to the experiment.
        """
        choices = envelope.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        reason = choices[0].get("finish_reason")
        return reason if isinstance(reason, str) else None

    @staticmethod
    def _extract_usage(envelope: dict) -> TokenUsage | None:
        usage = envelope.get("usage")
        if not isinstance(usage, dict):
            return None
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning_tokens is None:
            reasoning_tokens = usage.get("reasoning_tokens")
        return TokenUsage(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            reasoning_tokens=reasoning_tokens,
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", usage.get("prompt_cache_hit_tokens")),
        )


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        return "<unreadable error body>"

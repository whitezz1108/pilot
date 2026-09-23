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
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

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
    with urllib.request.urlopen(request, timeout=timeout) as handle:  # noqa: S310
        return handle.read()


def _redact(text: str, secret: str | None) -> str:
    """Remove a credential from anything that might be shown or logged."""
    if secret:
        text = text.replace(secret, "***REDACTED***")
    return text


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


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
        self._transport: Transport = transport or _urlopen_transport
        self._extra_headers = dict(extra_headers or {})

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    # -- request construction (pure; exercised by tests without a network) ---

    def build_request(self, request: ModelRequest) -> urllib.request.Request:
        """Turn a :class:`ModelRequest` into an HTTP request.

        Kept separate from :meth:`generate` so the exact wire payload can be
        asserted in a test with no transport at all.
        """
        params: ModelParams = request.params
        payload: dict[str, object] = {
            "model": params.model_id,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": params.temperature,
            "max_tokens": params.max_output_tokens,
        }
        if params.top_p is not None:
            payload["top_p"] = params.top_p
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}

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
        http_request = self.build_request(request)
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
            raise ModelClientError(
                f"could not reach the model endpoint {self.endpoint}: "
                f"{_redact(str(exc.reason), self._api_key)}"
            ) from exc
        except TimeoutError as exc:
            raise ModelClientError(
                f"model endpoint {self.endpoint} timed out after "
                f"{request.params.timeout_seconds}s"
            ) from exc

        responded_at = datetime.now(timezone.utc)
        envelope = self._decode(body)

        return ModelResponse(
            text=self._extract_text(envelope),
            provider=self._provider,
            model_id=str(envelope.get("model") or request.params.model_id),
            requested_at=requested_at,
            responded_at=responded_at,
            request_id=envelope.get("id"),
            usage=self._extract_usage(envelope),
            finish_reason=self._extract_finish_reason(envelope),
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
        return TokenUsage(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        return "<unreadable error body>"

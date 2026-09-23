"""Gate 2: the model-client boundary.

The boundary is small on purpose, and these tests pin all of it:

* :class:`~pilot01.model.client.ModelParams` is the reproducibility record --
  frozen, closed, and a stable fingerprint.
* :class:`~pilot01.model.scripted.ScriptedModelClient` replays declared text and
  refuses to invent a response. Every model-backed test in the suite runs on it.
* :class:`~pilot01.model.openai_compat.OpenAICompatibleClient` is the only live
  adapter. Its request construction is asserted with no transport at all, its
  response decoding with an injected fake transport, and its credential handling
  is checked to keep the key out of every message it can raise.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from pydantic import ValidationError

from pilot01.model import (
    ModelClientError,
    ModelMessage,
    ModelParams,
    ModelRequest,
    ModelResponse,
    ScriptedModelClient,
    TokenUsage,
)
from pilot01.model.client import ModelClient
from pilot01.model.openai_compat import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL_ENV,
    OpenAICompatibleClient,
)

DUMMY_KEY = "unit-test-key-not-a-real-credential"
BASE_URL = "https://example.invalid/v1"


def params(**overrides) -> ModelParams:
    base = dict(
        provider="openai-compatible",
        model_id="test-model-1",
        temperature=0.0,
        max_output_tokens=512,
    )
    base.update(overrides)
    return ModelParams(**base)


def request_for(messages=None, **overrides) -> ModelRequest:
    base = dict(
        messages=messages
        or (
            ModelMessage(role="system", content="SYSTEM"),
            ModelMessage(role="user", content="USER"),
        ),
        params=params(),
        role="manager",
        prompt_version="manager_v1",
        invocation=0,
        run_id="run-0001",
    )
    base.update(overrides)
    return ModelRequest(**base)


def envelope(text: str, **overrides) -> bytes:
    body = {
        "id": "chatcmpl-123",
        "model": "test-model-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


# --------------------------------------------------------------------------
# ModelParams
# --------------------------------------------------------------------------


def test_params_are_frozen_and_closed():
    record = params()
    with pytest.raises(ValidationError):
        record.temperature = 0.9
    with pytest.raises(ValidationError):
        ModelParams(
            provider="p",
            model_id="m",
            temperature=0.0,
            max_output_tokens=1,
            api_key="nope",
        )


def test_params_reject_out_of_range_values():
    with pytest.raises(ValidationError):
        params(temperature=3.0)
    with pytest.raises(ValidationError):
        params(max_output_tokens=0)
    with pytest.raises(ValidationError):
        params(top_p=0.0)


def test_params_fingerprint_identifies_the_parameter_set():
    assert params().fingerprint() == (
        "openai-compatible:test-model-1/T=0/top_p=default/max=512"
    )
    assert "top_p=0.9" in params(top_p=0.9).fingerprint()
    assert params(temperature=0.7).fingerprint() != params().fingerprint()


def test_top_p_defaults_to_none_meaning_provider_default():
    assert params().top_p is None


def test_request_rendered_input_contains_every_message_in_order():
    rendered = request_for().rendered_input()
    assert rendered.index("SYSTEM") < rendered.index("USER")
    assert rendered.startswith("### system")


def test_a_request_carries_no_credential_field():
    assert "api_key" not in ModelRequest.model_fields
    assert "api_key" not in ModelParams.model_fields
    assert "authorization" not in {name.lower() for name in ModelRequest.model_fields}


# --------------------------------------------------------------------------
# ScriptedModelClient
# --------------------------------------------------------------------------


def test_scripted_client_replays_declared_responses_in_order():
    client = ScriptedModelClient(("first", "second"))
    assert client.generate(request_for()).text == "first"
    assert client.generate(request_for()).text == "second"
    assert client.call_count == 2


def test_scripted_client_records_every_request_it_served():
    client = ScriptedModelClient(("only",))
    sent = request_for(role="compliance", prompt_version="compliance_v1")
    client.generate(sent)
    assert client.requests == (sent,)


def test_scripted_client_refuses_to_invent_a_response():
    client = ScriptedModelClient(("only",))
    client.generate(request_for())
    with pytest.raises(ModelClientError, match="no response for call 1"):
        client.generate(request_for())


def test_scripted_repeat_last_is_opt_in():
    client = ScriptedModelClient(("only",), repeat_last=True)
    assert client.generate(request_for()).text == "only"
    assert client.generate(request_for()).text == "only"
    assert client.call_count == 2


def test_an_empty_script_is_rejected():
    with pytest.raises(ValueError, match="at least one response"):
        ScriptedModelClient(())


def test_scripted_client_rejects_a_non_request():
    client = ScriptedModelClient(("only",))
    with pytest.raises(ModelClientError, match="expected ModelRequest"):
        client.generate({"messages": []})


def test_scripted_response_echoes_the_requested_model_identity():
    client = ScriptedModelClient(("x",))
    response = client.generate(request_for(params=params(model_id="echoed-model")))
    assert response.model_id == "echoed-model"
    assert response.provider == "openai-compatible"


def test_scripted_response_reports_a_non_negative_latency():
    client = ScriptedModelClient(("x",))
    response = client.generate(request_for())
    assert response.latency_seconds >= 0.0
    assert response.responded_at >= response.requested_at


def test_scripted_client_satisfies_the_protocol():
    assert isinstance(ScriptedModelClient(("x",)), ModelClient)


def test_scripted_client_reports_declared_usage():
    usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    client = ScriptedModelClient(("x",), usage=usage)
    assert client.generate(request_for()).usage == usage


# --------------------------------------------------------------------------
# OpenAICompatibleClient: configuration and credentials
# --------------------------------------------------------------------------


def test_a_live_client_requires_a_base_url(monkeypatch):
    monkeypatch.delenv(DEFAULT_BASE_URL_ENV, raising=False)
    with pytest.raises(ModelClientError, match="no base URL"):
        OpenAICompatibleClient(api_key=DUMMY_KEY)


def test_a_live_client_requires_a_key(monkeypatch):
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    with pytest.raises(ModelClientError, match="no API key"):
        OpenAICompatibleClient(base_url=BASE_URL)


def test_a_live_client_reads_its_key_from_the_environment(monkeypatch):
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, DUMMY_KEY)
    client = OpenAICompatibleClient(base_url=BASE_URL)
    assert DUMMY_KEY in client.build_request(request_for()).headers["Authorization"]


def test_constructing_a_live_client_reads_nothing_until_asked(monkeypatch):
    """The adapter is inert until a caller names it; no import does this."""
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_BASE_URL_ENV, raising=False)
    assert OpenAICompatibleClient is not None  # importable without a credential


# --------------------------------------------------------------------------
# OpenAICompatibleClient: request construction (no transport involved)
# --------------------------------------------------------------------------


def test_the_wire_request_targets_the_chat_completions_endpoint():
    client = OpenAICompatibleClient(base_url=BASE_URL, api_key=DUMMY_KEY)
    built = client.build_request(request_for())
    assert built.full_url == f"{BASE_URL}/chat/completions"
    assert built.method == "POST"
    assert built.get_header("Content-type") == "application/json"


def test_the_wire_request_carries_the_parameters_and_messages():
    client = OpenAICompatibleClient(base_url=BASE_URL, api_key=DUMMY_KEY)
    built = client.build_request(
        request_for(params=params(temperature=0.25, max_output_tokens=77, top_p=0.5))
    )
    payload = json.loads(built.data.decode("utf-8"))
    assert payload["model"] == "test-model-1"
    assert payload["temperature"] == 0.25
    assert payload["max_tokens"] == 77
    assert payload["top_p"] == 0.5
    assert payload["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert payload["response_format"] == {"type": "json_object"}


def test_top_p_is_omitted_when_it_is_not_set():
    client = OpenAICompatibleClient(base_url=BASE_URL, api_key=DUMMY_KEY)
    payload = json.loads(client.build_request(request_for()).data.decode("utf-8"))
    assert "top_p" not in payload


def test_json_mode_can_be_switched_off():
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, json_mode=False
    )
    payload = json.loads(client.build_request(request_for()).data.decode("utf-8"))
    assert "response_format" not in payload


def test_the_base_url_trailing_slash_does_not_double_up():
    client = OpenAICompatibleClient(base_url=f"{BASE_URL}/", api_key=DUMMY_KEY)
    assert client.build_request(request_for()).full_url == f"{BASE_URL}/chat/completions"


# --------------------------------------------------------------------------
# OpenAICompatibleClient: response decoding (injected transport)
# --------------------------------------------------------------------------


def test_generate_decodes_a_normal_envelope():
    seen: list = []

    def transport(http_request, timeout):
        seen.append((http_request, timeout))
        return envelope('{"ok": true}')

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    response = client.generate(request_for(params=params(timeout_seconds=12.5)))

    assert isinstance(response, ModelResponse)
    assert response.text == '{"ok": true}'
    assert response.request_id == "chatcmpl-123"
    assert response.model_id == "test-model-1"
    assert response.usage == TokenUsage(
        prompt_tokens=11, completion_tokens=22, total_tokens=33
    )
    assert seen[0][1] == 12.5


def test_generate_tolerates_content_parts():
    body = json.dumps(
        {
            "model": "m",
            "choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}],
        }
    ).encode()
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: body
    )
    assert client.generate(request_for()).text == "ab"


def test_a_non_json_body_is_a_client_error():
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: b"<html>oops</html>"
    )
    with pytest.raises(ModelClientError, match="not JSON"):
        client.generate(request_for())


def test_an_error_envelope_is_a_client_error():
    body = json.dumps({"error": {"message": "rate limited", "type": "rate_limit"}}).encode()
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: body
    )
    with pytest.raises(ModelClientError, match="rate limited"):
        client.generate(request_for())


def test_an_envelope_with_no_choices_is_a_client_error():
    body = json.dumps({"model": "m", "choices": []}).encode()
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: body
    )
    with pytest.raises(ModelClientError, match="no choices"):
        client.generate(request_for())


def test_an_http_error_becomes_a_client_error():
    def transport(http_request, timeout):
        raise urllib.error.HTTPError(
            http_request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelClientError, match="HTTP 401"):
        client.generate(request_for())


def test_a_connection_failure_becomes_a_client_error():
    def transport(http_request, timeout):
        raise urllib.error.URLError("name or service not known")

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelClientError, match="could not reach"):
        client.generate(request_for())


def test_a_timeout_becomes_a_client_error():
    def transport(http_request, timeout):
        raise TimeoutError("timed out")

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelClientError, match="timed out after"):
        client.generate(request_for())


def test_the_credential_never_appears_in_a_raised_error():
    """A provider that echoes the key back must not get it into an exception."""

    def transport(http_request, timeout):
        raise urllib.error.HTTPError(
            http_request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(f'{{"error":"bad key {DUMMY_KEY}"}}'.encode()),
        )

    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=transport
    )
    with pytest.raises(ModelClientError) as excinfo:
        client.generate(request_for())
    assert DUMMY_KEY not in str(excinfo.value)
    assert "***REDACTED***" in str(excinfo.value)


def test_the_credential_never_appears_in_a_decoded_response():
    """Even a provider that echoes the key cannot get it into a ModelResponse."""
    body = envelope(f"my key is {DUMMY_KEY}")
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: body
    )
    response = client.generate(request_for())
    assert DUMMY_KEY in response.text  # the raw text is preserved verbatim ...
    assert "api_key" not in response.model_dump()  # ... but no field holds it


def test_generate_rejects_a_non_request():
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: envelope("x")
    )
    with pytest.raises(ModelClientError, match="expected ModelRequest"):
        client.generate("not a request")


def test_the_live_adapter_satisfies_the_protocol():
    client = OpenAICompatibleClient(
        base_url=BASE_URL, api_key=DUMMY_KEY, transport=lambda *_: envelope("x")
    )
    assert isinstance(client, ModelClient)

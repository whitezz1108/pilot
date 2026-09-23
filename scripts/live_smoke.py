"""OPTIONAL DEVELOPMENT SMOKE TEST -- NOT PART OF THE TEST SUITE.

This is not a pytest module and it is not collected by ``pytest``. It exists so
that a developer can confirm, by hand and once, that the one live adapter can
actually reach a real provider and come back with a well-formed response. It is
not part of Gate 2.5, nothing in the pilot depends on it, and no experimental
result is derived from it.

    PILOT01_SMOKE=1 \
    PILOT01_BASE_URL=https://api.example.com/v1 \
    PILOT01_API_KEY=... \
    .venv/Scripts/python.exe scripts/live_smoke.py

What it does, and what it deliberately does not:

* **One request.** A single tiny chat completion, a handful of output tokens,
  through :class:`~pilot01.model.openai_compat.OpenAICompatibleClient` -- the
  same adapter a live run would use, so the thing being smoked is the real path
  and not a copy of it.
* **Gated on an explicit flag.** Without ``PILOT01_SMOKE=1`` it prints what it
  would do and exits non-zero, having sent nothing. Setting the endpoint and key
  is not enough on its own: the flag is the opt-in, so a stray ``.env`` cannot
  cause a paid call.
* **No secrets in output.** The key is read from the environment, put in one
  header, and never printed. The response body is passed through the adapter's
  own redaction before anything is shown, and the assertion in
  :func:`pilot01.model.log.assert_no_secrets` is applied to the record.
* **No model selection, no benchmarking.** The model id is whatever the
  environment says; this script does not choose, compare or recommend one.
* **Not a test, not a fixture, not a dependency.** Nothing imports it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.model import ModelCallLog, ModelParams, ModelRequest, ModelMessage  # noqa: E402
from pilot01.model.client import ModelClientError  # noqa: E402
from pilot01.model.log import assert_no_secrets  # noqa: E402
from pilot01.model.openai_compat import (  # noqa: E402
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL_ENV,
    OpenAICompatibleClient,
)
from pilot01.model.parsing import extract_json_object, record_call  # noqa: E402

SMOKE_FLAG = "PILOT01_SMOKE"
MODEL_ID_ENV = "PILOT01_MODEL_ID"
"""Environment variables this script reads. No others, and none are written."""

PROMPT = 'Reply with exactly this JSON object and nothing else: {"ok": true}'


def main() -> int:
    if os.environ.get(SMOKE_FLAG) != "1":
        print(
            f"refusing to run: this is an optional live smoke test and it makes a "
            f"real, billable model call.\n"
            f"set {SMOKE_FLAG}=1 to opt in explicitly, along with "
            f"{DEFAULT_BASE_URL_ENV} and {DEFAULT_API_KEY_ENV}.",
            file=sys.stderr,
        )
        return 2

    model_id = os.environ.get(MODEL_ID_ENV, "gpt-4o-mini")
    params = ModelParams(
        provider="openai-compatible",
        model_id=model_id,
        temperature=0.0,
        max_output_tokens=32,
        timeout_seconds=30.0,
    )

    try:
        client = OpenAICompatibleClient()
    except ModelClientError as exc:
        # The adapter's own message names the variable to set, never the value.
        print(f"cannot build a live client: {exc}", file=sys.stderr)
        return 2

    request = ModelRequest(
        messages=(
            ModelMessage(role="system", content="You are a smoke test."),
            ModelMessage(role="user", content=PROMPT),
        ),
        params=params,
        role="smoke",
        prompt_version="smoke_v1",
        run_id="live-smoke",
    )

    print(f"endpoint: {client.endpoint}")
    print(f"model_id: {params.model_id}")
    print(f"fingerprint: {params.fingerprint()}")
    print("sending one request ...")

    log = ModelCallLog()
    try:
        response = client.generate(request)
    except ModelClientError as exc:
        print(f"the adapter could not complete the request: {exc}", file=sys.stderr)
        return 1

    record = log.append(
        record_call(
            index=0,
            request=request,
            prompt_version=request.prompt_version,
            attempt=0,
            is_repair=False,
            response=response,
        )
    )
    assert_no_secrets(record)

    print(f"provider: {response.provider}")
    print(f"request_id: {response.request_id}")
    print(f"latency_seconds: {response.latency_seconds}")
    print(f"raw_response: {response.text!r}")

    try:
        payload = extract_json_object(response.text)
    except ValueError as exc:
        print(f"the response was not a JSON object: {exc}", file=sys.stderr)
        return 1

    print(f"parsed: {payload}")
    print(f"ok: the adapter completed one request at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A deterministic transport double.

:class:`ScriptedModelClient` is the model-layer counterpart of
:class:`~pilot01.workflow.nodes.script.FakeAgentScript`, and it exists for the
same reason: a test must be able to state exactly what came back, rather than
have a stand-in guess.

It replays declared raw response *text*. It does not parse, validate, repair, or
reinterpret anything -- those happen in :mod:`pilot01.model.parsing`, on the
same path a live response takes. A test that scripts malformed JSON therefore
exercises the real repair machinery, not a simulation of it.

Two deliberate strictness choices, both mirroring ``FakeAgentScript``:

* running out of scripted responses raises rather than falling back to a
  default, so an unexpectedly repeated call is a loud failure;
* ``repeat_last`` is opt-in, for the loop tests that intentionally re-invoke a
  node.

Every request is retained in :attr:`ScriptedModelClient.requests`, which is how
the isolation tests inspect what an agent node actually tried to send.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from .client import (
    ModelClientError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)

__all__ = ["ScriptedModelClient"]


def _fixed_step_clock(start: datetime, step_seconds: float = 0.5) -> Callable[[], datetime]:
    """A clock that advances by a fixed step on every call.

    Gives scripted calls a non-zero, reproducible latency so latency plumbing is
    exercised without making tests time-dependent.
    """
    state = {"now": start}

    def tick() -> datetime:
        current = state["now"]
        state["now"] = current + timedelta(seconds=step_seconds)
        return current

    return tick


class ScriptedModelClient:
    """Replays declared response texts. Makes no network call, ever."""

    def __init__(
        self,
        outputs: Sequence[str],
        *,
        clock: Callable[[], datetime] | None = None,
        repeat_last: bool = False,
        usage: TokenUsage | None = None,
        request_id_prefix: str = "scripted",
        provider: str | None = None,
        model_id: str | None = None,
    ) -> None:
        if not outputs:
            raise ValueError(
                "a scripted model client must declare at least one response; an "
                "empty script would silently produce a call with no answer"
            )
        self._outputs = tuple(outputs)
        self._repeat_last = repeat_last
        self._usage = usage
        self._request_id_prefix = request_id_prefix
        self._provider_override = provider
        self._model_id_override = model_id
        self._requests: list[ModelRequest] = []
        self._responses: list[ModelResponse] = []
        if clock is None:
            clock = _fixed_step_clock(datetime(2024, 1, 1, tzinfo=timezone.utc))
        self._clock = clock

    # -- inspection --------------------------------------------------------

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """Every request this client was asked to serve, in order."""
        return tuple(self._requests)

    @property
    def responses(self) -> tuple[ModelResponse, ...]:
        """Every response it returned, in order."""
        return tuple(self._responses)

    @property
    def call_count(self) -> int:
        return len(self._requests)

    @property
    def outputs(self) -> tuple[str, ...]:
        return self._outputs

    # -- ModelClient -------------------------------------------------------

    def generate(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise ModelClientError(
                f"scripted client received {type(request).__name__}; expected ModelRequest"
            )
        index = len(self._requests)
        if index < len(self._outputs):
            text = self._outputs[index]
        elif self._repeat_last:
            text = self._outputs[-1]
        else:
            raise ModelClientError(
                f"scripted client has no response for call {index}; the script "
                f"declares {len(self._outputs)} response(s) and repeat_last is "
                "False. Declare the response explicitly rather than letting the "
                "double fall back to a default."
            )

        self._requests.append(request)
        requested_at = self._clock()
        responded_at = self._clock()
        response = ModelResponse(
            text=text,
            provider=self._provider_override or request.params.provider,
            model_id=self._model_id_override or request.params.model_id,
            requested_at=requested_at,
            responded_at=responded_at,
            request_id=f"{self._request_id_prefix}-{index}",
            usage=self._usage,
        )
        self._responses.append(response)
        return response

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScriptedModelClient(responses={len(self._outputs)}, "
            f"calls={len(self._requests)}, repeat_last={self._repeat_last})"
        )

"""Fixture-controlled output scripts for the fake agents.

The fake Manager and Compliance are **pipeline fixtures, not behavioural
simulators**. Each one replays exactly the output a test declared, in order, and
infers nothing from what it receives.

That is an experimental-validity constraint, not a shortcut. Whether an upstream
omission is read downstream as ``absent``, as ``unknown``, or is recovered as
``present`` is the empirical outcome the real agents must produce; so is whether
an agent that *may* consult sources actually does. Encoding any of those
readings here -- even as a "reasonable default" -- would manufacture the very
effect the pilot exists to measure, and would do it invisibly, because the
result would look like a property of the workflow rather than of a fixture.

So the fakes carry no reasoning at all:

* they do not read the analyst memo, the policy, or the governance flags to
  decide what to say;
* what they say is a plain value the test supplied;
* an undeclared invocation fails loudly instead of falling back to a default.

Real behaviour arrives in Gate 2 behind the same
:class:`~pilot01.workflow.transitions.Node` interface, and the comparison
between "what the fixture declared" and "what the model produced" is the
measurement.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from ..transitions import ProtocolError

__all__ = ["FakeAgentScript"]


class FakeAgentScript(BaseModel):
    """An ordered, fixture-declared sequence of agent outputs.

    ``outputs[i]`` is replayed on the node's ``i``-th invocation. With
    ``repeat_last=False`` (the default) an invocation beyond the end of the
    script raises, so a workflow that calls a fake more often than the test
    intended cannot silently reuse a stale output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outputs: tuple[Any, ...]
    repeat_last: bool = False

    @model_validator(mode="after")
    def _require_declared_outputs(self) -> "FakeAgentScript":
        if not self.outputs:
            raise ValueError(
                "a fake agent script must declare at least one output; the fakes "
                "infer nothing, so an empty script has nothing to replay"
            )
        for index, output in enumerate(self.outputs):
            if not isinstance(output, BaseModel):
                raise ValueError(
                    f"scripted output {index} is {type(output).__name__}; expected a "
                    "pydantic model such as ManagerOutput or ComplianceOutput"
                )
        return self

    def at(self, invocation: int, *, expected: type[BaseModel], node: str) -> BaseModel:
        """Return the output declared for ``invocation``.

        Raises :class:`ProtocolError` if the script does not cover that
        invocation, or if the declared output is not the type this node must
        produce.
        """
        if invocation < len(self.outputs):
            output = self.outputs[invocation]
        elif self.repeat_last:
            output = self.outputs[-1]
        else:
            raise ProtocolError(
                f"fake {node} has no scripted output for invocation {invocation}; "
                f"the script declares {len(self.outputs)} output(s) and "
                "repeat_last is False. Declare the output explicitly rather than "
                "letting the fake fall back to a default."
            )
        if not isinstance(output, expected):
            raise ProtocolError(
                f"fake {node} was scripted with {type(output).__name__} at "
                f"invocation {invocation}; expected {expected.__name__}"
            )
        return output

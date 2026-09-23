"""Explicit node-transition mechanism.

The point of this module is that a node never chooses its own successor. A
node returns a :class:`NodeResult` carrying an *intent* -- a
:class:`TransitionKind` -- and the runner resolves the destination from the
workflow's edge table (see :mod:`pilot01.config`).

Pilot v1 uses only ``default``. The revision feedback loop

    manager <- compliance   (kind: request_revision)

is a future extension that is expressed purely as (a) an extra edge in a later
``workflow_v*.yaml`` and (b) ``max_revision_rounds > 0``. Enabling it requires
no change to the runner's control flow, which is why the resolution logic for
it already exists here and is rejected -- not absent -- while the loop is off.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "END",
    "TransitionKind",
    "Transition",
    "NodeResult",
    "Node",
    "NodeRegistry",
    "ProtocolError",
    "WorkflowConfigError",
    "UnknownTransitionError",
    "FeedbackLoopDisabledError",
]

END = "end"
"""Sentinel destination meaning "terminate the run"."""


class ProtocolError(RuntimeError):
    """The run violated the workflow protocol.

    Raised for structural violations -- a node asked to run out of order, a
    restricted view that cannot be built, a feedback transition while the loop
    is disabled. A protocol error aborts the run rather than being smoothed
    over, because silently continuing would corrupt the experiment.
    """


class WorkflowConfigError(ValueError):
    """The workflow definition is internally inconsistent."""


class UnknownTransitionError(WorkflowConfigError):
    """A node requested a transition kind it has no edge for."""


class FeedbackLoopDisabledError(WorkflowConfigError):
    """A revision was requested while ``max_revision_rounds`` is 0."""


class TransitionKind(str, Enum):
    DEFAULT = "default"
    REQUEST_REVISION = "request_revision"


class Transition(BaseModel):
    """A resolved transition: what the runner actually did.

    Recorded in the event log so the executed route is auditable without
    re-deriving it from the config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TransitionKind
    source: str
    target: str


class NodeResult(BaseModel):
    """What a node returns to the runner.

    ``output`` is intentionally untyped: its concrete type differs per node
    (``AnalystMemo`` for the analyst, ``ManagerOutput``/``ComplianceOutput``
    for the agents) and each node validates its own output before returning.
    The runner stores it on the state and publishes it in the event log.
    """

    model_config = ConfigDict(extra="forbid")

    output: Any = None
    transition: TransitionKind = TransitionKind.DEFAULT


@dataclass(frozen=True)
class Node:
    """A workflow node: how to build its input, and what it does with it.

    ``build_input`` is supplied by the runner with the full state and the
    invocation index. For agent nodes it must return a
    :class:`~pilot01.workflow.views.RestrictedView`; the runner enforces that
    before dispatch, so a node cannot be handed the full state by accident.
    """

    name: str
    build_input: Callable[[Any, int], Any]
    run: Callable[[Any], NodeResult]
    apply: Callable[[Any, Any], None]
    """Write this node's output into the state. Owned by the node, not the runner."""
    requires_restricted_view: bool = True
    output_model: type[BaseModel] | None = None
    """When set, the runner rejects any output that is not an instance of it."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("node name must not be empty")


class NodeRegistry:
    """Name -> :class:`Node`. Explicit, ordered, no dynamic discovery."""

    def __init__(self, nodes: Iterable[Node]) -> None:
        self._nodes: dict[str, Node] = {}
        for node in nodes:
            if node.name in self._nodes:
                raise ProtocolError(f"duplicate node registration: {node.name!r}")
            self._nodes[node.name] = node

    def get(self, name: str) -> Node:
        try:
            return self._nodes[name]
        except KeyError:
            raise ProtocolError(
                f"no node registered under {name!r}; registered: {sorted(self._nodes)}"
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def names(self) -> tuple[str, ...]:
        return tuple(self._nodes)

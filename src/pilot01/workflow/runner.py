"""The sequential workflow runner.

The runner owns control flow; nodes own behaviour. It never branches on the
experimental condition, never reads gold, and never decides a route on its own:
it asks the node for a transition *intent* and resolves the destination from
the workflow's edge table.

Per step it:

1. builds the node's input via the node's own ``build_input``;
2. enforces isolation -- an agent node must be handed a
   :class:`~pilot01.workflow.views.RestrictedView`, and that view is audited for
   leaked hidden data before dispatch;
3. logs the input, runs the node, validates the output type, logs the output;
4. applies the output to the state and logs the resolved transition.

Any structural violation raises and is logged as ``protocol_error``: the run
fails loudly rather than producing a quietly corrupted observation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..config import ConditionsConfig, WorkflowConfig
from ..events import Event, EventCollector, EventType, utc_now
from .export import ExecutionRecord, build_execution_record
from .state import ExperimentState, RunStatus
from .transitions import (
    END,
    Node,
    NodeRegistry,
    NodeResult,
    ProtocolError,
    TransitionKind,
    WorkflowConfigError,
)
from .views import RestrictedView, assert_view_clean

__all__ = ["RunOutcome", "WorkflowRunner"]


class RunOutcome(BaseModel):
    """Result of one run: the final state plus the audit trail.

    ``state`` carries hidden gold, so **this model is not an export format**.
    Persist a run through :meth:`to_execution_record`; see
    :mod:`pilot01.workflow.export` for why the distinction is enforced in code
    rather than left to discipline.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    final_node: str
    steps_executed: int
    state: ExperimentState
    events: tuple[Event, ...]

    def to_execution_record(self) -> ExecutionRecord:
        """The public, gold-free record of this run. The supported export path."""
        return build_execution_record(self)

    def to_jsonl(self) -> str:
        """The audit trail as JSONL. Gold-free: the log never carried gold."""
        return "".join(f"{event.model_dump_json()}\n" for event in self.events)


def _dump(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class WorkflowRunner:
    """Executes one :class:`ExperimentState` through a workflow definition."""

    def __init__(
        self,
        *,
        workflow: WorkflowConfig,
        conditions: ConditionsConfig,
        registry: NodeRegistry,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._workflow = workflow
        self._conditions = conditions
        self._registry = registry
        self._clock = clock or utc_now
        self._invocations: dict[str, int] = {}
        self._collector: EventCollector | None = None

    @property
    def workflow(self) -> WorkflowConfig:
        return self._workflow

    @property
    def last_events(self) -> tuple[Event, ...]:
        """The audit trail of the most recent :meth:`run`, or ``()`` if none ran.

        Read-only, and deliberately a snapshot rather than the collector: the
        collector is append-only and private, and handing it out would let a
        caller append to a finished run's record.

        This exists because :meth:`run` re-raises on a structural violation, so
        a caller that wants to persist *why* a run failed -- which is every
        caller that runs a batch -- has no ``RunOutcome`` to read the trail
        from. The alternative would be to catch the exception and reconstruct
        the events from the exception's text, which would make the failure
        record a paraphrase rather than the record itself.
        """
        if self._collector is None:
            return ()
        return self._collector.events

    def run(self, state: ExperimentState) -> RunOutcome:
        """Run to completion.

        Raises :class:`ProtocolError` (or a :class:`WorkflowConfigError`
        subclass) on any structural violation, after logging a
        ``protocol_error`` event.
        """
        self._invocations = {}
        collector = EventCollector(state.run_id, clock=self._clock)
        self._collector = collector

        collector.append(
            EventType.RUN_STARTED,
            payload={
                "case_id": state.case_id,
                "repetition_id": state.repetition_id,
                "condition_id": state.condition_id,
                "error_condition": state.error_condition.value,
                "source_access": state.source_access,
                "verification_required": state.verification_required,
                "contract_id": state.contract_id,
                "contract_text_hash": state.contract_text_hash,
                "target_category": state.target_category,
                "memo_id": state.analyst_memo.memo_id,
                "memo_claim_count": len(state.analyst_memo.claims),
                "policy_id": state.policy.policy_id,
                "workflow_version": self._workflow.workflow_version,
                "experiment_version": state.experiment_version,
            },
        )

        state.status = RunStatus.RUNNING
        state.started_at = self._clock()
        current = self._workflow.start
        state.current_node = current
        steps = 0

        try:
            # Fail before the first node rather than mid-run: a state whose
            # treatment labels, flags, artifact and gold disagree is not a valid
            # observation, whatever the nodes would do with it. Checked inside
            # the try block so the rejection is recorded in the log.
            state.check_treatment_integrity(self._conditions)

            while current != END:
                # Defensive: WorkflowConfig already guarantees every reachable
                # node is declared. Kept so a future relaxation of that
                # validation cannot turn into a silent off-graph execution.
                if current not in self._workflow.nodes:
                    raise ProtocolError(
                        f"node {current!r} is not part of workflow version "
                        f"{self._workflow.workflow_version}"
                    )
                if steps >= self._workflow.max_steps:
                    raise ProtocolError(
                        f"run {state.run_id!r} exceeded max_steps="
                        f"{self._workflow.max_steps}; aborting"
                    )

                node = self._registry.get(current)
                invocation = self._invocations.get(current, 0)
                self._invocations[current] = invocation + 1
                steps += 1

                collector.append(
                    EventType.NODE_STARTED,
                    node=current,
                    payload={"step": steps, "invocation": invocation},
                )

                node_input = self._build_input(node, state, invocation)
                collector.append(
                    EventType.AGENT_INPUT_CREATED,
                    node=current,
                    payload={
                        "restricted": isinstance(node_input, RestrictedView),
                        "input": _dump(node_input),
                    },
                )

                result = node.run(node_input)
                if not isinstance(result, NodeResult):
                    raise ProtocolError(
                        f"node {current!r} returned {type(result).__name__}; "
                        "expected NodeResult"
                    )
                collector.append(
                    EventType.AGENT_OUTPUT_RECEIVED,
                    node=current,
                    payload={"output": _dump(result.output)},
                )

                self._validate_output(node, result)
                node.apply(state, result.output)
                collector.append(
                    EventType.NODE_COMPLETED,
                    node=current,
                    payload={"step": steps, "transition": result.transition.value},
                )

                target = self._workflow.resolve(current, result.transition)
                if result.transition is TransitionKind.REQUEST_REVISION:
                    state.revision_round += 1
                collector.append(
                    EventType.TRANSITION,
                    node=current,
                    payload={
                        "kind": result.transition.value,
                        "source": current,
                        "target": target,
                        "revision_round": state.revision_round,
                    },
                )
                state.current_node = target
                current = target

        except (ProtocolError, WorkflowConfigError) as exc:
            state.status = RunStatus.FAILED
            state.steps_executed = steps
            collector.append(
                EventType.PROTOCOL_ERROR,
                node=state.current_node or None,
                payload={"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise

        state.status = RunStatus.COMPLETED
        state.completed_at = self._clock()
        state.steps_executed = steps
        collector.append(
            EventType.RUN_COMPLETED,
            payload={
                "steps": steps,
                "final_node": END,
                "final_decision": (
                    state.compliance_output.decision.value
                    if state.compliance_output is not None
                    else None
                ),
                "revision_round": state.revision_round,
            },
        )
        return RunOutcome(
            run_id=state.run_id,
            status=state.status,
            final_node=END,
            steps_executed=steps,
            state=state,
            events=collector.events,
        )

    # -- internals ---------------------------------------------------------

    def _build_input(self, node: Node, state: ExperimentState, invocation: int):
        """Build a node's input and enforce the isolation contract."""
        node_input = node.build_input(state, invocation)
        if node.requires_restricted_view:
            if not isinstance(node_input, RestrictedView):
                raise ProtocolError(
                    f"agent node {node.name!r} was handed "
                    f"{type(node_input).__name__}; agent nodes must receive a "
                    "RestrictedView, never the full ExperimentState"
                )
            assert_view_clean(node_input)
        elif isinstance(node_input, RestrictedView):
            raise ProtocolError(
                f"artifact-loader node {node.name!r} was handed a RestrictedView; "
                "loaders select artifacts from the treatment, they are not agents"
            )
        return node_input

    @staticmethod
    def _validate_output(node: Node, result: NodeResult) -> None:
        if node.output_model is None:
            return
        if not isinstance(result.output, node.output_model):
            raise ProtocolError(
                f"node {node.name!r} produced {type(result.output).__name__}; "
                f"expected {node.output_model.__name__}"
            )

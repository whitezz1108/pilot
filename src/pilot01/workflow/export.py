"""The safe persistence boundary.

:class:`~pilot01.workflow.state.ExperimentState` and
:class:`~pilot01.workflow.runner.RunOutcome` legitimately carry hidden gold:
the state *is* the single source of truth for a run, and scoring needs the
labels. That makes them dangerous to hand to anything that persists, logs,
caches or ships data -- a single ``RunOutcome.model_dump()`` writes the scoring
labels into the file the execution record was supposed to be free of, and
nothing about the call looks wrong.

This module makes the split explicit, so that "export a run" cannot mean
"serialize everything":

* :class:`ExecutionRecord` -- the public experimental record. Run identity,
  researcher-facing condition and case metadata, node outputs, event references
  and execution metadata. It has no field that could hold gold, and its
  validator walks its own serialization and refuses any key that looks like
  gold or omission data, so a future edit cannot quietly add one.
* :class:`ScoringRecord` -- the hidden registry: gold labels and omission
  provenance. Produced only by :func:`build_scoring_record`, a separate,
  explicitly scorer-only path that the execution export never calls.

The supported way to persist a run is :meth:`ExecutionRecord.write_jsonl` (or
``RunOutcome.to_execution_record()`` first). Serializing ``RunOutcome`` or
``ExperimentState`` directly is not an export.

No scorer is implemented here: this is the boundary only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator

from ..events import Event
from ..schemas import (
    ComplianceOutput,
    ErrorCondition,
    HiddenGold,
    ManagerOutput,
    OmissionRecord,
)
from .state import RunStatus
from .transitions import ProtocolError
from .views import iter_keys

if TYPE_CHECKING:  # import cycle: runner imports this module for the boundary
    from .runner import RunOutcome

__all__ = [
    "ExportLeakError",
    "GOLD_KEY_PATTERNS",
    "ExecutionRecord",
    "ScoringRecord",
    "build_execution_record",
    "build_scoring_record",
]


class ExportLeakError(ProtocolError):
    """A record destined for export carries hidden scoring data."""


GOLD_KEY_PATTERNS: tuple[str, ...] = ("gold", "hidden", "omission", "omit", "scoring")
"""Key substrings that must never appear anywhere in a public execution record.

Narrower than the restricted-view patterns in :mod:`pilot01.workflow.views`:
the execution record is researcher-facing, so treatment labels
(``error_condition``, ``condition_id``) belong in it. Only the scoring labels
are excluded.
"""


def _gold_key_violations(key: str) -> tuple[str, ...]:
    lowered = key.lower()
    return tuple(pattern for pattern in GOLD_KEY_PATTERNS if pattern in lowered)


class ExecutionRecord(BaseModel):
    """The public record of one run. Contains no scoring labels.

    Built by explicit whitelist in :func:`build_execution_record`; the whitelist
    is the code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- run identity ------------------------------------------------------
    run_id: str
    case_id: str
    repetition_id: int
    experiment_version: str
    workflow_version: str
    policy_version: str

    # -- condition / governance metadata (researcher-facing, not agent-facing)
    condition_id: str
    error_condition: ErrorCondition
    source_access: bool
    verification_required: bool

    # -- case metadata -----------------------------------------------------
    contract_id: str
    contract_text_hash: str
    target_category: str
    policy_id: str
    policy_target_clause_categories: tuple[str, ...]

    # -- node outputs ------------------------------------------------------
    manager_output: ManagerOutput | None = None
    compliance_output: ComplianceOutput | None = None

    # -- execution metadata ------------------------------------------------
    status: RunStatus
    final_node: str
    steps_executed: int
    revision_round: int
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # -- event references --------------------------------------------------
    events: tuple[Event, ...] = ()
    """The full audit trail, referenced by :attr:`Event.event_id`.

    Safe by construction: the runner never writes gold to the event log.
    """

    @model_validator(mode="after")
    def _refuse_hidden_keys(self) -> "ExecutionRecord":
        violations = [
            path
            for path, key in iter_keys(self.model_dump(mode="json"))
            if _gold_key_violations(key)
        ]
        if violations:
            raise ExportLeakError(
                "execution record would carry hidden scoring data at: "
                f"{'; '.join(violations)}; gold belongs in a ScoringRecord, which "
                "is produced only by build_scoring_record"
            )
        return self

    def event_ids(self) -> tuple[int, ...]:
        return tuple(event.event_id for event in self.events)

    def to_jsonl(self) -> str:
        """The audit trail as JSONL. Identical in content to ``RunOutcome.to_jsonl``."""
        return "".join(f"{event.model_dump_json()}\n" for event in self.events)

    def write_jsonl(self, path: str | Path) -> Path:
        """Append-free write of the event trail. Returns the path written."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_jsonl(), encoding="utf-8")
        return target


class ScoringRecord(BaseModel):
    """Scorer-only: the hidden labels for one run.

    Never produced by the execution export path. Nothing here may be joined
    back onto an :class:`ExecutionRecord` before the final analysis step.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    case_id: str
    repetition_id: int
    condition_id: str
    error_condition: ErrorCondition
    gold: HiddenGold
    omission: OmissionRecord | None = None


def build_execution_record(outcome: "RunOutcome") -> ExecutionRecord:
    """Project a run onto the public record.

    Every field is named explicitly. ``outcome.state.hidden`` is never read, so
    there is no code path by which gold could reach the record -- and the
    record's own validator refuses it even if one were added later.
    """
    state = outcome.state
    return ExecutionRecord(
        run_id=state.run_id,
        case_id=state.case_id,
        repetition_id=state.repetition_id,
        experiment_version=state.experiment_version,
        workflow_version=state.workflow_version,
        policy_version=state.policy_version,
        condition_id=state.condition_id,
        error_condition=state.error_condition,
        source_access=state.source_access,
        verification_required=state.verification_required,
        contract_id=state.contract_id,
        contract_text_hash=state.contract_text_hash,
        target_category=state.target_category,
        policy_id=state.policy.policy_id,
        policy_target_clause_categories=state.policy.target_clause_categories,
        manager_output=state.manager_output,
        compliance_output=state.compliance_output,
        status=outcome.status,
        final_node=outcome.final_node,
        steps_executed=outcome.steps_executed,
        revision_round=state.revision_round,
        started_at=state.started_at,
        completed_at=state.completed_at,
        events=outcome.events,
    )


def build_scoring_record(outcome: "RunOutcome") -> ScoringRecord:
    """The explicitly separate scorer-only path.

    Calling this is a deliberate act: it is the only function that moves hidden
    labels out of a run, and it is not reachable from
    :func:`build_execution_record`.
    """
    state = outcome.state
    return ScoringRecord(
        run_id=state.run_id,
        case_id=state.case_id,
        repetition_id=state.repetition_id,
        condition_id=state.condition_id,
        error_condition=state.error_condition,
        gold=state.hidden.gold,
        omission=state.hidden.omission,
    )

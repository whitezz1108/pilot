"""Executing a plan: one run at a time, one directory each, nothing overwritten.

The batch runner is deliberately thin. It does not decide anything experimental:
it takes a :class:`~pilot01.experiment.runspec.RunPlan`, and for each job builds
the state the job describes, wires the model-backed nodes around transports the
caller supplies, and hands the whole thing to the existing
:class:`~pilot01.workflow.runner.WorkflowRunner`. The runner remains the
authority for what happens inside a run; this module is the authority for which
runs happen, in what order, and what they leave behind.

Four rules, each of which is a decision rather than an implementation detail:

**Serial, in plan order.** No multiprocessing, no threads, no reordering. The
plan's order *is* the randomization, so a runner that shuffled or parallelised
would destroy the property the plan exists to establish. Serial execution is
also what makes one run's failure diagnosable without disentangling it from
another's.

**A run is never overwritten.** A directory holding ``run.json`` is a completed
observation. Re-running it silently would replace an observation with a second
one and leave no trace that the first existed. So the default is to refuse, and
``resume`` skips while ``rerun_failed`` moves the old run aside -- both explicit
acts.

**A partial run is moved aside, not reused.** A directory without ``run.json``
holds a run that died. Its ``model_calls.jsonl`` already has a ``call_index``
sequence belonging to that attempt, and appending a fresh attempt to it would
produce a log describing two runs at once. It is renamed to
``<run_id>.partial`` and kept.

**A failed run is still an observation.** The runner re-raises on a structural
violation, so a failed run produces no ``RunOutcome``. This module catches that
exception and persists the run anyway -- identity, the public execution record
built from the post-failure state, the audit trail the runner recorded, and a
derived :class:`~pilot01.experiment.artifacts.FailureRecord`. A batch that threw
away its failures would report a protocol failure as a missing row, which is
precisely the confusion §7 of the Gate 3 specification forbids.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..config import ConditionsConfig, ModelsConfig, WorkflowConfig
from ..model import ModelCallLog, ModelClient, ScriptedModelClient
from ..schemas import ErrorCondition
from ..source.tools import ToolCallLog
from ..source.verification import VerificationLog
from ..workflow.export import build_execution_record
from ..workflow.nodes import build_llm_registry
from ..workflow.runner import RunOutcome, WorkflowRunner
from ..workflow.state import ExperimentState
from ..workflow.transitions import ProtocolError, WorkflowConfigError
from .artifacts import (
    FailureRecord,
    RunArtifact,
    RunDirectory,
    derive_failure,
    read_raw_run,
)
from .cases import CaseRegistry
from .layout import ExperimentPaths
from .runspec import RunPlan, RunPlanError, RunSpec

__all__ = [
    "BatchError",
    "JobStatus",
    "JobResult",
    "BatchResult",
    "ClientFactory",
    "ScriptedClientFactory",
    "load_response_script",
    "ExperimentRunner",
    "plan_or_raise",
]


class BatchError(RuntimeError):
    """A batch cannot proceed: the plan, the outputs, or the wiring disagree."""


class JobStatus(str, Enum):
    """What happened to one job.

    ``SKIPPED`` and ``FAILED`` are different findings and are kept apart: the
    first means no observation was made, the second means an observation was
    made and it failed. A summary that merged them would report an unrun job as
    a failure rate.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RERUN = "rerun"


class JobResult(BaseModel):
    """The outcome of one job, as the batch saw it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    case_id: str
    condition_id: str
    error_condition: str
    repeat_index: int = Field(ge=0)

    status: JobStatus
    directory: str
    failure: FailureRecord | None = None
    detail: str | None = None
    """Why the job was skipped, or what went wrong. Descriptive only."""

    @property
    def produced_observation(self) -> bool:
        """Whether a raw run was written for this job."""
        return self.status is not JobStatus.SKIPPED


class BatchResult(BaseModel):
    """Every job's outcome, in plan order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    plan_fingerprint: str
    results: tuple[JobResult, ...]

    def of_status(self, status: JobStatus) -> tuple[JobResult, ...]:
        return tuple(result for result in self.results if result.status is status)

    @property
    def completed(self) -> tuple[JobResult, ...]:
        return self.of_status(JobStatus.COMPLETED)

    @property
    def failed(self) -> tuple[JobResult, ...]:
        return self.of_status(JobStatus.FAILED)

    @property
    def skipped(self) -> tuple[JobResult, ...]:
        return self.of_status(JobStatus.SKIPPED)

    @property
    def rerun(self) -> tuple[JobResult, ...]:
        return self.of_status(JobStatus.RERUN)

    @property
    def counts(self) -> dict[str, int]:
        return {
            status.value: len(self.of_status(status))
            for status in JobStatus
            if self.of_status(status)
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        return target


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class ClientFactory(Protocol):
    """Supplies one run's transports.

    A factory rather than a pair of clients, because a client is per-run state:
    a transport reused across runs would carry its own call cursor, its cache
    and any provider-side session from one observation into the next, which is
    exactly the cross-run contamination the design forbids. The batch runner
    calls this once per run, per role, and never reuses the result.

    ``role`` is one of :data:`~pilot01.config.REQUIRED_MODEL_ROLES`; ``job`` is
    the run the transport is for, so a scripted factory can look up the response
    it declared for that run.
    """

    def __call__(self, *, role: str, job: RunSpec) -> ModelClient: ...


def load_response_script(path: str | Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Read a ``--responses`` file: ``{run_id: {role: [response_text, ...]}}``.

    A development and test affordance, not a research one. It exists so the CLI
    can drive a whole experiment deterministically without a key and without a
    network, which is what makes the pipeline testable end to end.

    Validated structurally here -- shape, roles, non-empty scripts -- and against
    the plan by :class:`ScriptedClientFactory`, which is where a missing run can
    actually be detected.
    """
    source = Path(path)
    if not source.is_file():
        raise BatchError(f"no response script at {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BatchError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchError(
            f"{source} must contain an object mapping run ids to role responses"
        )

    script: dict[str, dict[str, tuple[str, ...]]] = {}
    for run_id, roles in payload.items():
        if not isinstance(roles, dict) or not roles:
            raise BatchError(
                f"{source}: run {run_id!r} must map at least one role to a list of "
                "response texts"
            )
        entries: dict[str, tuple[str, ...]] = {}
        for role, responses in roles.items():
            if isinstance(responses, str):
                raise BatchError(
                    f"{source}: run {run_id!r} role {role!r} must be a list of "
                    "response texts, not a single string"
                )
            if not isinstance(responses, list) or not responses:
                raise BatchError(
                    f"{source}: run {run_id!r} role {role!r} declares no responses; "
                    "an empty script would produce a call with no answer"
                )
            if not all(isinstance(item, str) for item in responses):
                raise BatchError(
                    f"{source}: run {run_id!r} role {role!r} contains a non-string "
                    "response"
                )
            entries[str(role)] = tuple(responses)
        script[str(run_id)] = entries
    return script


class ScriptedClientFactory:
    """A deterministic factory over a declared response script.

    Every run gets fresh :class:`~pilot01.model.scripted.ScriptedModelClient`
    objects, so the script for one run cannot be advanced by another. A run the
    script does not mention raises rather than falling back to a default: a
    batch that quietly served an undeclared run would produce an observation
    whose responses nobody wrote.
    """

    def __init__(
        self,
        script: Mapping[str, Mapping[str, Sequence[str]]],
        *,
        repeat_last: bool = False,
    ) -> None:
        self._script = {
            str(run_id): {str(role): tuple(texts) for role, texts in roles.items()}
            for run_id, roles in script.items()
        }
        self._repeat_last = repeat_last

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(self._script)

    def __call__(self, *, role: str, job: RunSpec) -> ModelClient:
        roles = self._script.get(job.run_id)
        if roles is None:
            raise BatchError(
                f"the response script declares no responses for run {job.run_id!r}; "
                f"it covers {len(self._script)} run(s)"
            )
        texts = roles.get(role)
        if texts is None:
            raise BatchError(
                f"the response script declares no responses for role {role!r} of run "
                f"{job.run_id!r}; it declares {sorted(roles)}"
            )
        return ScriptedModelClient(texts, repeat_last=self._repeat_last)


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


class ExperimentRunner:
    """Runs every job of a plan, serially, into one raw tree."""

    def __init__(
        self,
        *,
        plan: RunPlan,
        registry: CaseRegistry,
        conditions: ConditionsConfig,
        workflow: WorkflowConfig,
        models: ModelsConfig,
        client_factory: ClientFactory,
        paths: ExperimentPaths,
        clock: Callable[[], datetime] | None = None,
        progress: Callable[[int, int, JobResult], None] | None = None,
    ) -> None:
        self._plan = plan
        self._registry = registry
        self._conditions = conditions
        self._workflow = workflow
        self._models = models
        self._client_factory = client_factory
        self._paths = paths
        self._clock = clock
        self._progress = progress

    @property
    def plan(self) -> RunPlan:
        return self._plan

    @property
    def paths(self) -> ExperimentPaths:
        return self._paths

    def run(
        self,
        *,
        resume: bool = False,
        rerun_failed: bool = False,
        fail_fast: bool = False,
    ) -> BatchResult:
        """Execute the plan.

        ``resume`` skips jobs whose run directory already holds a ``run.json``,
        and clears partial directories out of the way. ``rerun_failed``
        additionally re-runs the jobs whose completed run failed, moving the old
        attempt aside first. ``fail_fast`` stops at the first protocol failure --
        useful when bringing up a live provider, where the first failure
        usually explains all of them.
        """
        if rerun_failed and not resume:
            raise BatchError(
                "rerun_failed requires resume: re-running failed jobs while "
                "refusing to look at what is already on disk would leave the two "
                "settings disagreeing about the same directory"
            )
        # A plan read back from disk is an input like any other, and gets the
        # same scrutiny as one just built.
        self._plan.check(self._conditions, self._registry)

        self._paths.ensure()
        results: list[JobResult] = []
        total = len(self._plan.jobs)

        for position, job in enumerate(self._plan.jobs, start=1):
            result = self._run_one(job, resume=resume, rerun_failed=rerun_failed)
            results.append(result)
            if self._progress is not None:
                self._progress(position, total, result)
            if fail_fast and result.status is JobStatus.FAILED:
                break

        return BatchResult(
            experiment_id=self._plan.experiment_id,
            plan_fingerprint=self._plan.fingerprint(),
            results=tuple(results),
        )

    # -- one job -----------------------------------------------------------

    def _run_one(
        self, job: RunSpec, *, resume: bool, rerun_failed: bool
    ) -> JobResult:
        directory = RunDirectory(self._paths.run_dir(job.run_id))
        status = JobStatus.COMPLETED
        detail: str | None = None

        if directory.is_complete():
            artifact = self._existing_artifact(directory)
            if rerun_failed and artifact.failed:
                directory.move_aside()
                status = JobStatus.RERUN
            elif resume:
                return self._result(
                    job,
                    JobStatus.SKIPPED,
                    directory,
                    detail=f"already complete ({artifact.status.value})",
                    failure=artifact.failure,
                )
            else:
                raise BatchError(
                    f"{directory.path} already holds a completed run. Refusing to "
                    "overwrite an observation: pass --resume to keep it, or "
                    "--resume --rerun-failed to replace a failed attempt."
                )
        elif directory.exists():
            if not resume:
                raise BatchError(
                    f"{directory.path} holds an interrupted run (no run.json). Pass "
                    "--resume to move it aside and run this job again."
                )
            moved = directory.move_aside()
            detail = f"moved interrupted attempt to {moved.name}" if moved else None

        artifact = self._execute(job, directory)
        if artifact.failed:
            status = JobStatus.FAILED
        return self._result(job, status, directory, detail=detail, failure=artifact.failure)

    def _execute(self, job: RunSpec, directory: RunDirectory) -> RunArtifact:
        """Build the state, run it, and persist everything the run produced."""
        case = self._registry.get(job.case_id)
        directory.prepare()

        state = ExperimentState.create(
            run_id=job.run_id,
            case_id=job.case_id,
            repetition_id=job.repeat_index,
            condition_id=job.condition_id,
            conditions=self._conditions,
            error_condition=job.error_condition,
            contract_id=job.contract_id,
            contract_text_hash=case.contract_text_hash,
            target_category=case.target_category,
            memo=self._registry.memo(job.case_id, job.error_condition),
            gold_status=case.gold_clause_status,
            policy=case.policy,
            gold_evidence_offsets=case.gold_evidence_offsets,
            omission=(
                None
                if job.error_condition is ErrorCondition.E0
                else self._registry.omission_record(job.case_id)
            ),
            experiment_version=self._plan.experiment_version,
            workflow_version=self._workflow.workflow_version,
        )

        # Fresh logs per run, each bound to its own file. The call and tool logs
        # append as the run proceeds, so a run that dies still leaves the calls
        # it made; the verification log is written once at the end, because a
        # verdict is derived from tool calls that must all be in first.
        call_log = ModelCallLog(path=directory.model_calls)
        tool_log = ToolCallLog(path=directory.tool_calls)
        verification_log = VerificationLog()

        registry = build_llm_registry(
            repository=self._registry.repository,
            models=self._models,
            manager_client=self._client_factory(role="manager", job=job),
            compliance_client=self._client_factory(role="compliance", job=job),
            call_log=call_log,
            source=self._registry.library,
            tool_log=tool_log,
            verification_log=verification_log,
        )
        runner = WorkflowRunner(
            workflow=self._workflow,
            conditions=self._conditions,
            registry=registry,
            clock=self._clock,
        )

        try:
            outcome = runner.run(state)
        except (ProtocolError, WorkflowConfigError):
            # The runner re-raised after marking the state failed and recording
            # the violation. Rebuild its own result type from the post-failure
            # state and the trail it kept, rather than writing a second
            # ExecutionRecord builder here: the supported export path is
            # build_execution_record, and a parallel one would be a second
            # definition of what an execution record is.
            outcome = RunOutcome(
                run_id=state.run_id,
                status=state.status,
                final_node=state.current_node,
                steps_executed=state.steps_executed,
                state=state,
                events=runner.last_events,
            )

        execution = build_execution_record(outcome)
        failure = derive_failure(
            events=outcome.events,
            model_calls=call_log.records,
            tool_calls=tool_log.records,
            verifications=verification_log.records,
        )

        directory.write_verification(verification_log.records)
        directory.write_events(execution)

        artifact = RunArtifact(
            experiment_id=job.experiment_id,
            run_id=job.run_id,
            case_id=job.case_id,
            contract_id=job.contract_id,
            condition_id=job.condition_id,
            error_condition=job.error_condition,
            source_access=job.source_access,
            verification_required=job.verification_required,
            repeat_index=job.repeat_index,
            plan_version=self._plan.plan_version,
            plan_fingerprint=self._plan.fingerprint(),
            models_version=job.models_version,
            model_fingerprint=job.model_fingerprint,
            execution=execution,
            failure=failure,
            model_calls=len(call_log.records),
            tool_calls=len(tool_log.records),
            verifications=len(verification_log.records),
        )
        directory.write_run(artifact)
        return artifact

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _existing_artifact(directory: RunDirectory) -> RunArtifact:
        return read_raw_run(directory.path).artifact

    @staticmethod
    def _result(
        job: RunSpec,
        status: JobStatus,
        directory: RunDirectory,
        *,
        detail: str | None = None,
        failure: FailureRecord | None = None,
    ) -> JobResult:
        return JobResult(
            run_id=job.run_id,
            case_id=job.case_id,
            condition_id=job.condition_id,
            error_condition=job.error_condition.value,
            repeat_index=job.repeat_index,
            status=status,
            directory=str(directory.path),
            failure=failure,
            detail=detail,
        )


def plan_or_raise(path: str | Path) -> RunPlan:
    """Load a plan, translating a read failure into the batch's own error type."""
    try:
        return RunPlan.load_json(path)
    except RunPlanError as exc:
        raise BatchError(str(exc)) from exc

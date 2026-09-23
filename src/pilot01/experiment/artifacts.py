"""What one run leaves on disk, and how it is read back.

A run's raw directory is the primary record of the experiment. Everything the
processed layer reports is derived from these files, and nothing in them is
derived from anything else -- so a raw tree can be re-scored, years later, by
code that has never seen the plan that produced it.

Five files, and the reason each is separate:

``model_calls.jsonl``
    The private raw-call log. Appended live by
    :class:`~pilot01.model.log.ModelCallLog`, so a run that dies mid-flight
    still leaves the calls it made.
``tool_calls.jsonl``
    The source-tool log, appended live for the same reason.
``verification.jsonl``
    The V1 verdicts. Written once at the end: a verdict is derived from the
    tool calls, so it can only be reached after they are all in.
``events.jsonl``
    The workflow audit trail, from the run's own
    :class:`~pilot01.workflow.export.ExecutionRecord`.
``run.json``
    The run artifact: identity, the public execution record, and the derived
    failure record.

**``run.json`` is written last, and atomically.** Its presence is therefore the
definition of a complete run -- a directory holding four log files and no
``run.json`` is a run that was interrupted, and the batch runner treats it as
such rather than mistaking it for an observation. The write goes to a temporary
name and is then ``os.replace``-d into place, so a process killed mid-write
leaves either the old file or the new one and never a half-written one.

**The failure record is derived, not declared.** Nothing about a run's outcome
is decided by the code that ran it: :func:`derive_failure` reads the events, the
model calls, the tool calls and the verification verdicts, and classifies. A
run that fails and a run that completes are distinguished by evidence in the
artifacts, which is what makes the classification auditable after the fact.

**The artifact carries no gold.** It is built from an
:class:`~pilot01.workflow.export.ExecutionRecord`, which has no gold field and
refuses one, and the envelope's own validator re-checks the whole serialization
for gold-shaped keys. Gold reaches the scorer from the registry, never from
here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..events import Event, EventType
from ..model.log import ModelCallRecord
from ..schemas import ErrorCondition
from ..source.tools import ToolCallRecord
from ..source.verification import VerificationOutcome
from ..workflow.export import GOLD_KEY_PATTERNS, ExecutionRecord
from ..workflow.state import RunStatus
from ..workflow.views import iter_keys

__all__ = [
    "ARTIFACT_VERSION",
    "PARTIAL_SUFFIX",
    "RUN_JSON",
    "MODEL_CALLS_JSONL",
    "TOOL_CALLS_JSONL",
    "VERIFICATION_JSONL",
    "EVENTS_JSONL",
    "ArtifactError",
    "FailureRecord",
    "RunArtifact",
    "RunDirectory",
    "RawRun",
    "derive_failure",
    "read_raw_run",
    "iter_raw_runs",
]


ARTIFACT_VERSION = "1"
PARTIAL_SUFFIX = ".partial"

RUN_JSON = "run.json"
MODEL_CALLS_JSONL = "model_calls.jsonl"
TOOL_CALLS_JSONL = "tool_calls.jsonl"
VERIFICATION_JSONL = "verification.jsonl"
EVENTS_JSONL = "events.jsonl"

LOG_FILES: tuple[str, ...] = (
    MODEL_CALLS_JSONL,
    TOOL_CALLS_JSONL,
    VERIFICATION_JSONL,
    EVENTS_JSONL,
)


class ArtifactError(RuntimeError):
    """A raw run artifact is missing, malformed, or inconsistent."""


def _gold_key_violations(key: str) -> tuple[str, ...]:
    lowered = key.lower()
    return tuple(pattern for pattern in GOLD_KEY_PATTERNS if pattern in lowered)


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


class FailureRecord(BaseModel):
    """Why a run did not produce a usable observation, read off its artifacts.

    Every field is a statement about evidence, not a judgement about severity.
    ``protocol_failure`` is true when the workflow itself stopped; the three
    specific flags say which layer stopped it, and they are not exclusive --
    a run can fail verification *and* have a tool call refused.

    The distinction the scorer depends on is between ``protocol_failure`` (the
    run produced no usable output because the machinery stopped) and a run that
    completed with a wrong answer. Those are different findings, and a metric
    that collapsed them would report a protocol failure as an error the agent
    made.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_failure: bool = False
    failure_type: str | None = None
    """The exception class name the runner recorded, e.g. ``ProtocolError``."""
    failure_stage: str | None = None
    """The node the run was in when it stopped."""
    error: str | None = None
    """The runner's own message. Descriptive only; nothing branches on it."""

    verification_failure: bool = False
    verification_failures: tuple[str, ...] = ()
    """The distinct :class:`~pilot01.source.verification.VerificationFailure` values."""

    tool_failure: bool = False
    tool_failures: int = 0

    model_output_failure: bool = False
    """The model never produced a parseable object, even after its one repair."""


def derive_failure(
    *,
    events: Iterable[Event],
    model_calls: Iterable[ModelCallRecord],
    tool_calls: Iterable[ToolCallRecord],
    verifications: Iterable[VerificationOutcome],
) -> FailureRecord:
    """Classify a run's failure from its raw artifacts.

    Deliberately reads the *typed* records rather than the runner's message
    text: a classification that matched on an error string would change meaning
    the next time somebody reworded an exception, and would silently start
    classifying nothing at all.
    """
    events = tuple(events)
    model_calls = tuple(model_calls)
    tool_calls = tuple(tool_calls)
    verifications = tuple(verifications)

    protocol_events = [
        event for event in events if event.event_type is EventType.PROTOCOL_ERROR
    ]
    protocol_failure = bool(protocol_events)
    failure_type = failure_stage = error = None
    if protocol_events:
        last = protocol_events[-1]
        failure_type = last.payload.get("error_type")
        failure_stage = last.node
        error = last.payload.get("error")

    failed_verifications = [record for record in verifications if record.failures]
    verification_failures: list[str] = []
    for record in failed_verifications:
        for failure in record.failures:
            if failure.value not in verification_failures:
                verification_failures.append(failure.value)

    failed_tools = [record for record in tool_calls if not record.ok]

    # A model-output failure is the case where the workflow stopped and neither
    # verification nor the tool layer explains why: the model simply never
    # produced an object the schema accepted. A transport error counts too --
    # both are "the model did not answer", as opposed to "the answer was
    # rejected".
    model_output_failure = protocol_failure and not verification_failures and any(
        record.parse_error or record.error for record in model_calls
    )

    return FailureRecord(
        protocol_failure=protocol_failure,
        failure_type=failure_type,
        failure_stage=failure_stage,
        error=error,
        verification_failure=bool(verification_failures),
        verification_failures=tuple(verification_failures),
        tool_failure=bool(failed_tools),
        tool_failures=len(failed_tools),
        model_output_failure=model_output_failure,
    )


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


class RunArtifact(BaseModel):
    """One run, as persisted. Identity plus the public record plus the failure.

    The identity block repeats what the plan said about this job, rather than
    referencing it. That is what lets the raw tree be scored on its own: a
    directory of runs carries its own labels, and the scorer never has to be
    handed the manifest to know what it is looking at.

    ``execution`` is the existing
    :class:`~pilot01.workflow.export.ExecutionRecord`, unchanged. Wrapping it
    rather than extending it keeps the gold-free guarantee exactly where Gate 2
    put it -- the record's own validator still runs, and this envelope does not
    have to re-derive its field list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_version: str = ARTIFACT_VERSION

    experiment_id: str
    run_id: str

    case_id: str
    contract_id: str

    condition_id: str
    error_condition: ErrorCondition
    source_access: bool
    verification_required: bool

    repeat_index: int = Field(ge=0)

    plan_version: str
    plan_fingerprint: str
    models_version: str
    model_fingerprint: str

    execution: ExecutionRecord
    failure: FailureRecord | None = None

    # -- counts, so a run can be triaged without opening four log files ----

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    verifications: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "RunArtifact":
        if self.execution.run_id != self.run_id:
            raise ArtifactError(
                f"run artifact {self.run_id!r} wraps an execution record for "
                f"{self.execution.run_id!r}"
            )
        if self.execution.case_id != self.case_id:
            raise ArtifactError(
                f"run artifact {self.run_id!r} names case {self.case_id!r} but its "
                f"execution record names {self.execution.case_id!r}"
            )
        if self.execution.condition_id != self.condition_id:
            raise ArtifactError(
                f"run artifact {self.run_id!r} names condition {self.condition_id!r} "
                f"but its execution record names {self.execution.condition_id!r}"
            )
        if self.execution.error_condition is not self.error_condition:
            raise ArtifactError(
                f"run artifact {self.run_id!r} is labelled {self.error_condition.value} "
                f"but its execution record is labelled "
                f"{self.execution.error_condition.value}"
            )
        if self.execution.source_access is not self.source_access:
            raise ArtifactError(
                f"run artifact {self.run_id!r} carries source_access="
                f"{self.source_access} but its execution record carries "
                f"{self.execution.source_access}"
            )
        if self.execution.verification_required is not self.verification_required:
            raise ArtifactError(
                f"run artifact {self.run_id!r} carries verification_required="
                f"{self.verification_required} but its execution record carries "
                f"{self.execution.verification_required}"
            )
        if self.execution.repetition_id != self.repeat_index:
            raise ArtifactError(
                f"run artifact {self.run_id!r} is repeat {self.repeat_index} but its "
                f"execution record says repetition_id={self.execution.repetition_id}"
            )
        violations = [
            path
            for path, key in iter_keys(self.model_dump(mode="json"))
            if _gold_key_violations(key)
        ]
        if violations:
            raise ArtifactError(
                "run artifact would carry hidden scoring data at: "
                f"{'; '.join(violations)}; gold belongs to the registry and to the "
                "scorer, never to a raw run file"
            )
        return self

    @property
    def status(self) -> RunStatus:
        return self.execution.status

    @property
    def completed(self) -> bool:
        return self.execution.status is RunStatus.COMPLETED

    @property
    def failed(self) -> bool:
        return self.failure is not None and self.failure.protocol_failure

    def final_decision(self):
        """The decision the run produced, or ``None`` if it produced none."""
        output = self.execution.compliance_output
        return None if output is None else output.decision

    def write_json(self, path: str | Path) -> Path:
        """Write atomically. A reader never sees a half-written artifact."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        return target

    @classmethod
    def from_payload(cls, payload: object) -> "RunArtifact":
        if not isinstance(payload, dict):
            raise ArtifactError("a run artifact file must contain a JSON object")
        version = payload.get("artifact_version")
        if version != ARTIFACT_VERSION:
            raise ArtifactError(
                f"artifact_version {version!r} is not the supported version "
                f"{ARTIFACT_VERSION!r}"
            )
        return cls.model_validate(payload)


# --------------------------------------------------------------------------
# The directory
# --------------------------------------------------------------------------


class RunDirectory:
    """The on-disk home of one run's raw artifacts.

    Owns the file names so that the writer and the reader cannot disagree about
    them, and owns the one ordering rule that matters: the logs are opened
    before the run starts, and ``run.json`` is written after it ends.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_json(self) -> Path:
        return self._path / RUN_JSON

    @property
    def model_calls(self) -> Path:
        return self._path / MODEL_CALLS_JSONL

    @property
    def tool_calls(self) -> Path:
        return self._path / TOOL_CALLS_JSONL

    @property
    def verification(self) -> Path:
        return self._path / VERIFICATION_JSONL

    @property
    def events(self) -> Path:
        return self._path / EVENTS_JSONL

    def is_complete(self) -> bool:
        """Whether this directory holds a finished run.

        ``run.json`` is written last and atomically, so its presence is exactly
        the condition -- no timestamp comparison, no size heuristic.
        """
        return self.run_json.is_file()

    def exists(self) -> bool:
        return self._path.exists()

    def prepare(self) -> None:
        self._path.mkdir(parents=True, exist_ok=True)

    def move_aside(self) -> Path | None:
        """Move an interrupted run's directory out of the way.

        Called when a directory exists without ``run.json``: it holds a partial
        run, and reusing it would append this run's model calls to a log whose
        ``call_index`` already belongs to another attempt. The partial is kept
        as ``<run_id>.partial`` rather than deleted, because "the run died
        here" is itself worth being able to look at -- and an older partial is
        replaced, since two partials of the same run are never both useful.
        """
        if not self._path.exists():
            return None
        target = self._path.with_name(self._path.name + PARTIAL_SUFFIX)
        if target.exists():
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                else:
                    child.rmdir()
            target.rmdir()
        os.replace(self._path, target)
        return target

    def write_verification(self, outcomes: Iterable[VerificationOutcome]) -> Path:
        """Write the verification verdicts. Called once, after the run."""
        self.prepare()
        lines = "".join(f"{outcome.model_dump_json()}\n" for outcome in outcomes)
        self.verification.write_text(lines, encoding="utf-8")
        return self.verification

    def write_events(self, record: ExecutionRecord) -> Path:
        """Write the workflow audit trail from the public execution record."""
        self.prepare()
        return record.write_jsonl(self.events)

    def write_run(self, artifact: RunArtifact) -> Path:
        """Write ``run.json``. Always the last write of a run."""
        return artifact.write_json(self.run_json)


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


class RawRun:
    """One raw run, fully loaded: the artifact and the four logs behind it.

    Read whole rather than lazily. A raw run is small -- four JSONL files of a
    few hundred lines -- and the scorer wants all of it, so a lazy reader would
    buy nothing and would let a caller score a run from a partial view.
    """

    def __init__(
        self,
        *,
        directory: RunDirectory,
        artifact: RunArtifact,
        model_calls: tuple[ModelCallRecord, ...],
        tool_calls: tuple[ToolCallRecord, ...],
        verifications: tuple[VerificationOutcome, ...],
        events: tuple[Event, ...],
    ) -> None:
        self.directory = directory
        self.artifact = artifact
        self.model_calls = model_calls
        self.tool_calls = tool_calls
        self.verifications = verifications
        self.events = events

    @property
    def run_id(self) -> str:
        return self.artifact.run_id

    @property
    def path(self) -> Path:
        return self.directory.path

    def model_calls_of(self, role: str) -> tuple[ModelCallRecord, ...]:
        return tuple(record for record in self.model_calls if record.role == role)

    def tool_calls_of(self, node: str) -> tuple[ToolCallRecord, ...]:
        return tuple(record for record in self.tool_calls if record.node == node)

    def verifications_of(self, node: str) -> tuple[VerificationOutcome, ...]:
        return tuple(record for record in self.verifications if record.node == node)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RawRun({self.run_id!r}, status={self.artifact.status.value}, "
            f"model_calls={len(self.model_calls)}, tool_calls={len(self.tool_calls)})"
        )


def _read_jsonl(path: Path, model: Any, label: str) -> tuple:
    """Parse a JSONL file into models. A missing file is empty, not an error.

    A run with no tool calls legitimately has an empty tool log, and the log
    files are created before the run starts -- so absence means "this log was
    never opened", which is a run that died before its first write, and reads
    as the empty list it is.
    """
    if not path.is_file():
        return ()
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except Exception as exc:  # noqa: BLE001 - re-raised with the location
            raise ArtifactError(
                f"{path} line {number} is not a valid {label}: {exc}"
            ) from exc
    return tuple(records)


def read_raw_run(path: str | Path) -> RawRun:
    """Load one raw run directory in full.

    Raises :class:`ArtifactError` if the directory is not a complete run. A
    partial directory is not a run, and scoring one would mean reporting a
    truncated execution as an observation.
    """
    directory = RunDirectory(path)
    if not directory.path.is_dir():
        raise ArtifactError(f"no raw run directory at {directory.path}")
    if not directory.is_complete():
        raise ArtifactError(
            f"{directory.path} holds no {RUN_JSON}; it is an interrupted run, not an "
            "observation, and must not be scored"
        )
    try:
        payload = json.loads(directory.run_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{directory.run_json} is not valid JSON: {exc}") from exc
    artifact = RunArtifact.from_payload(payload)

    return RawRun(
        directory=directory,
        artifact=artifact,
        model_calls=_read_jsonl(directory.model_calls, ModelCallRecord, "model call"),
        tool_calls=_read_jsonl(directory.tool_calls, ToolCallRecord, "tool call"),
        verifications=_read_jsonl(
            directory.verification, VerificationOutcome, "verification outcome"
        ),
        events=_read_jsonl(directory.events, Event, "event"),
    )


def iter_raw_runs(root: str | Path) -> Iterator[RawRun]:
    """Every complete run under a raw-experiment root, in run-id order.

    Skips partial directories and non-directories, so pointing this at a tree
    that a killed batch left behind yields the runs that finished rather than
    an exception about the one that did not. Sorted, because a scoring pass
    that iterated in filesystem order would produce a differently-ordered
    result file on a different machine.
    """
    base = Path(root)
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.endswith(PARTIAL_SUFFIX):
            continue
        if not (child / RUN_JSON).is_file():
            continue
        yield read_raw_run(child)

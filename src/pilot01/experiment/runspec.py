"""The run unit, and the frozen plan that enumerates every one of them.

The runtime unit of the experiment is ``case x error condition x governance
condition x repeat``. A :class:`RunSpec` is one such job, and a
:class:`RunPlan` is the complete, ordered, immutable list of them for one
experiment.

Three properties are worth more than the rest, and each is enforced rather than
intended:

**Stable identity.** :func:`make_run_id` builds the run id out of the job's own
experimental identity, so the id is a *function* of what the job is. Two
manifests that describe the same job produce the same id; two jobs that differ
in any identity component produce different ids. There is no counter, no clock
and no random component, which is why a plan can be rebuilt byte-for-byte from
its inputs and why a resumed batch can tell whether it is looking at the run it
planned.

**Reproducible order.** The order is randomized -- a fixed order would let a
slow drift in the environment land on one condition -- but the randomization is
driven by :func:`_shuffle`, which is Fisher-Yates over
``random.Random(seed).random()``. That one method is the part of :mod:`random`
CPython guarantees to be stable across versions and platforms, so the order is a
property of the manifest rather than of the interpreter that read it. A
different seed reorders the plan and changes nothing about its membership.

**Interleaving.** The plan is dealt round-robin across ``(case, error
condition)`` cells rather than concatenated cell by cell. Concatenating would
put every governance condition of one case next to the others, which is exactly
the arrangement that lets a transient provider problem land on one case instead
of being spread across all of them.

**Randomization never touches treatment content.** The only thing a seed can
change is the *order* of the jobs. Contract text, memos, prompts, condition
flags and gold labels are all fixed before the plan is built and are copied into
each :class:`RunSpec` unchanged -- the plan carries them, it does not produce
them.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ConditionsConfig, ModelsConfig, REQUIRED_MODEL_ROLES
from ..schemas import ErrorCondition
from .cases import CaseRegistry, CaseRegistryError, is_safe_component

__all__ = [
    "PLAN_VERSION",
    "EXPERIMENT_VERSION",
    "RUN_ID_SEPARATOR",
    "RunPlanError",
    "RunSpec",
    "RunPlan",
    "make_run_id",
    "model_fingerprint",
    "build_run_plan",
    "write_run_plan_csv",
    "RUN_PLAN_COLUMNS",
]


PLAN_VERSION = "1"
"""Version of the on-disk run-plan format. Bumped when its shape changes."""

EXPERIMENT_VERSION = "1"
"""Version of the experiment protocol the plan belongs to.

Stamped into every run's state and carried on the plan, so that a raw tree
records which protocol produced it. It is a different thing from
:data:`PLAN_VERSION`: the plan format could change while the protocol did not,
and the protocol could change -- a new metric definition, a changed repeat
semantics -- while the plan format stayed the same.
"""

RUN_ID_SEPARATOR = "__"

RUN_PLAN_COLUMNS: tuple[str, ...] = (
    "position",
    "run_id",
    "experiment_id",
    "case_id",
    "contract_id",
    "condition_id",
    "error_condition",
    "source_access",
    "verification_required",
    "repeat_index",
    "models_version",
    "model_fingerprint",
)


class RunPlanError(RuntimeError):
    """A run plan is internally inconsistent, or cannot be read."""


def model_fingerprint(models: ModelsConfig) -> str:
    """A stable identity for the model configuration a plan is run under.

    Every role's parameter set, in :data:`~pilot01.config.REQUIRED_MODEL_ROLES`
    order, so the string is comparable across runs and does not depend on the
    order a config file happened to list its roles in. Two plans whose
    fingerprints differ are not the same experiment, whatever else they share.
    """
    parts = [
        f"{role}={models.for_role(role).fingerprint()}" for role in REQUIRED_MODEL_ROLES
    ]
    return f"models_v{models.models_version}|" + ";".join(parts)


def make_run_id(
    *,
    experiment_id: str,
    case_id: str,
    error_condition: ErrorCondition,
    condition_id: str,
    repeat_index: int,
) -> str:
    """Build the run id from the job's experimental identity.

    Readable on purpose. A run id is researcher-facing -- it names a directory
    of raw artifacts and appears in the execution record alongside the condition
    -- so an operator scanning ``outputs/raw/`` can see what each directory is
    without opening it. It is never rendered into a prompt: a test asserts that
    no model request contains one.

    The identity tuple is unique by construction, because a plan refuses to hold
    two jobs with the same ``(case, error condition, condition, repeat)``, so
    the id is collision-free without needing a counter or a hash.
    """
    if repeat_index < 0:
        raise RunPlanError(f"repeat_index must be non-negative, got {repeat_index}")
    components = {
        "experiment_id": experiment_id,
        "case_id": case_id,
        "condition_id": condition_id,
    }
    for name, value in components.items():
        if not is_safe_component(value):
            raise RunPlanError(
                f"{name} {value!r} is not usable as a run-id component; it must be "
                "non-empty and free of whitespace, path separators and '..'"
            )
    return RUN_ID_SEPARATOR.join(
        [
            experiment_id,
            case_id,
            error_condition.value,
            condition_id,
            f"r{repeat_index:03d}",
        ]
    )


class RunSpec(BaseModel):
    """One experimental job: what to run, under which treatment, for which repeat.

    Identity only -- there is no gold field here, and there is no field one could
    be put in. A spec names a case; the case's labels stay in the registry, where
    only the state builder and the scorer read them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    experiment_id: str

    case_id: str
    contract_id: str

    condition_id: str
    error_condition: ErrorCondition
    source_access: bool
    verification_required: bool

    repeat_index: int = Field(ge=0)

    models_version: str
    model_fingerprint: str

    @property
    def cell(self) -> tuple[str, str, str, int]:
        """The experimental cell this job occupies: ``(case, error, condition, repeat)``."""
        return (
            self.case_id,
            self.error_condition.value,
            self.condition_id,
            self.repeat_index,
        )

    @property
    def block(self) -> tuple[str, str]:
        """The ``(case, error condition)`` group this job belongs to.

        Used by the interleaver: jobs sharing a block are the same case under the
        same memo arm, differing only in governance, and the whole point of
        dealing round-robin is to keep them apart in the run order.
        """
        return (self.case_id, self.error_condition.value)

    def identity(self) -> tuple[str, str, str, str, int]:
        return (
            self.experiment_id,
            self.case_id,
            self.error_condition.value,
            self.condition_id,
            self.repeat_index,
        )


class RunPlan(BaseModel):
    """The frozen, ordered run plan for one experiment.

    Ordering is part of the plan's identity: two plans with the same jobs in a
    different order are different plans, because the order is what the
    randomization is for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_version: str = PLAN_VERSION
    experiment_version: str = EXPERIMENT_VERSION
    experiment_id: str
    seed: int
    repeat_count: int = Field(ge=1)
    condition_ids: tuple[str, ...]
    case_ids: tuple[str, ...]
    models_version: str
    model_fingerprint: str
    jobs: tuple[RunSpec, ...]

    @model_validator(mode="after")
    def _check_shape(self) -> "RunPlan":
        """The invariants that need nothing but the plan itself.

        Deliberately *not* "the job count equals cases x conditions x repeats".
        That formula assumes every case runs both error conditions, and a
        negative sentinel does not: it declares the correct-memo arm alone,
        because there is no claim to omit. Completeness is therefore a statement
        about the registry, and is checked in :meth:`check`, which has it. What
        is checked here is everything a plan can be held to on its own.
        """
        if not self.jobs:
            raise RunPlanError("a run plan must contain at least one job")
        seen: set[str] = set()
        for job in self.jobs:
            if job.run_id in seen:
                raise RunPlanError(f"duplicate run_id {job.run_id!r} in plan")
            seen.add(job.run_id)
            if job.experiment_id != self.experiment_id:
                raise RunPlanError(
                    f"job {job.run_id!r} belongs to experiment {job.experiment_id!r}, "
                    f"plan is {self.experiment_id!r}"
                )
            if job.repeat_index >= self.repeat_count:
                raise RunPlanError(
                    f"job {job.run_id!r} has repeat_index {job.repeat_index}, which is "
                    f"outside the plan's {self.repeat_count} repeat(s)"
                )
        duplicates = self.duplicate_cells()
        if duplicates:
            raise RunPlanError(
                f"plan occupies {len(duplicates)} cell(s) more than once, first: "
                f"{duplicates[0]}"
            )
        return self

    # -- inspection --------------------------------------------------------

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(job.run_id for job in self.jobs)

    def by_run_id(self, run_id: str) -> RunSpec:
        for job in self.jobs:
            if job.run_id == run_id:
                return job
        raise RunPlanError(f"no job {run_id!r} in this plan")

    def cells(self) -> tuple[tuple[str, str, str, int], ...]:
        return tuple(job.cell for job in self.jobs)

    def missing_cells(
        self, registry: CaseRegistry
    ) -> tuple[tuple[str, str, str, int], ...]:
        """Cells the plan should occupy but does not.

        The registry is required, and not merely for convenience: which error
        conditions a case runs is a property of the case -- a negative sentinel
        runs the correct-memo arm only, because it has no claim to omit -- so
        "every expected cell" cannot be computed from the plan's header alone.
        Deriving it from a fixed ``(E0, E1)`` pair would report a sentinel's
        absent arm as a missing cell, which is the opposite of true.
        """
        expected = {
            (case_id, error.value, condition_id, repeat)
            for case_id in self.case_ids
            for error in registry.get(case_id).error_conditions
            for condition_id in self.condition_ids
            for repeat in range(self.repeat_count)
        }
        present = set(self.cells())
        return tuple(sorted(expected - present))

    def duplicate_cells(self) -> tuple[tuple[str, str, str, int], ...]:
        """Cells occupied by more than one job."""
        counts: dict[tuple[str, str, str, int], int] = {}
        for job in self.jobs:
            counts[job.cell] = counts.get(job.cell, 0) + 1
        return tuple(sorted(cell for cell, count in counts.items() if count > 1))

    def fingerprint(self) -> str:
        """A digest over the plan's identity *and its order*.

        The order is included, because the order is the randomization and the
        randomization is part of the design. A resume compares this against the
        plan it is resuming so that "the same experiment" cannot quietly mean
        "the same jobs in a different sequence".
        """
        material = json.dumps(
            {
                "plan_version": self.plan_version,
                "experiment_version": self.experiment_version,
                "experiment_id": self.experiment_id,
                "seed": self.seed,
                "repeat_count": self.repeat_count,
                "condition_ids": list(self.condition_ids),
                "case_ids": list(self.case_ids),
                "models_version": self.models_version,
                "model_fingerprint": self.model_fingerprint,
                "jobs": [list(job.identity()) for job in self.jobs],
            },
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict:
        return json.loads(self.model_dump_json())

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2) + "\n", encoding="utf-8"
        )
        return target

    @classmethod
    def from_payload(cls, payload: object) -> "RunPlan":
        if not isinstance(payload, dict):
            raise RunPlanError("a run plan file must contain a JSON object")
        version = payload.get("plan_version")
        if version != PLAN_VERSION:
            raise RunPlanError(
                f"plan_version {version!r} is not the supported version {PLAN_VERSION!r}"
            )
        return cls.model_validate(payload)

    @classmethod
    def load_json(cls, path: str | Path) -> "RunPlan":
        source = Path(path)
        if not source.is_file():
            raise RunPlanError(f"no run plan at {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunPlanError(f"{source} is not valid JSON: {exc}") from exc
        return cls.from_payload(payload)

    def check(self, conditions: ConditionsConfig, registry: CaseRegistry) -> None:
        """Assert the plan is a faithful expansion of its own inputs.

        Called by :func:`build_run_plan` and again by the batch runner, because a
        plan read back from disk is an input like any other and gets the same
        scrutiny as one just built.
        """
        for job in self.jobs:
            if job.condition_id not in self.condition_ids:
                raise RunPlanError(
                    f"job {job.run_id!r} uses condition {job.condition_id!r}, which the "
                    "plan's header does not list"
                )
            if not conditions.is_main(job.condition_id):
                raise RunPlanError(
                    f"job {job.run_id!r} uses {job.condition_id!r}, which is not a "
                    f"permitted main condition ({list(conditions.main_conditions)})"
                )
            condition = conditions.get(job.condition_id)
            if job.source_access is not condition.source_access:
                raise RunPlanError(
                    f"job {job.run_id!r} carries source_access={job.source_access} but "
                    f"condition {job.condition_id} declares {condition.source_access}"
                )
            if job.verification_required is not condition.verification_required:
                raise RunPlanError(
                    f"job {job.run_id!r} carries verification_required="
                    f"{job.verification_required} but condition {job.condition_id} "
                    f"declares {condition.verification_required}"
                )
            case = registry.get(job.case_id)
            if not case.runs(job.error_condition):
                raise RunPlanError(
                    f"job {job.run_id!r} runs case {job.case_id!r} under "
                    f"{job.error_condition.value}, which the case does not declare"
                )
            if job.contract_id != case.contract_id:
                raise RunPlanError(
                    f"job {job.run_id!r} names contract {job.contract_id!r} but case "
                    f"{job.case_id!r} is about {case.contract_id!r}"
                )
            expected_id = make_run_id(
                experiment_id=job.experiment_id,
                case_id=job.case_id,
                error_condition=job.error_condition,
                condition_id=job.condition_id,
                repeat_index=job.repeat_index,
            )
            if job.run_id != expected_id:
                raise RunPlanError(
                    f"job id {job.run_id!r} is not the id its identity derives "
                    f"({expected_id!r})"
                )
        missing = self.missing_cells(registry)
        if missing:
            raise RunPlanError(
                f"plan is missing {len(missing)} expected cell(s), first: {missing[0]}"
            )
        duplicates = self.duplicate_cells()
        if duplicates:
            raise RunPlanError(
                f"plan occupies {len(duplicates)} cell(s) more than once, first: "
                f"{duplicates[0]}"
            )


# --------------------------------------------------------------------------
# Randomization
#
# Both helpers take an explicit ``random.Random`` so the draw sequence is a
# function of the seed and of the order the calls are made in, and of nothing
# else. Neither touches the contents of a job.
# --------------------------------------------------------------------------


def _shuffle(items: list, rng: random.Random) -> None:
    """In-place Fisher-Yates driven by ``random.Random.random()``.

    Deliberately not :func:`random.shuffle`. CPython guarantees that
    ``random.Random(seed).random()`` reproduces across versions and platforms;
    it guarantees nothing about ``shuffle``'s implementation, and a plan whose
    order changed under a Python upgrade would be a reproducibility bug that
    only shows up years later, in the archived data.
    """
    for index in range(len(items) - 1, 0, -1):
        swap = int(rng.random() * (index + 1))
        items[index], items[swap] = items[swap], items[index]


def _interleave(blocks: list[list[RunSpec]], rng: random.Random) -> list[RunSpec]:
    """Deal the per-block job lists round-robin, reshuffling the block order each round.

    Round-robin rather than concatenation, for the reason in the module
    docstring: concatenating whole blocks would put a case's governance
    conditions next to each other, so any transient condition of the environment
    would fall on one case rather than being spread over all of them.

    Each block is shuffled *before* dealing, and that is not decoration. Dealing
    takes one job per block per round, so the round a job lands in is its index
    within its block: with a fixed within-block order every block would hand over
    its condition 1 job in round 1, its condition 2 job in round 2, and so on,
    making the condition a job was run under a function of how far into the batch
    it ran. A provider that degraded halfway through the run would then have
    degraded exactly one condition. Shuffling each block breaks that, and the
    block order is reshuffled every round as well so the case dimension is not
    tied to position either.
    """
    order = list(blocks)
    for block in order:
        _shuffle(block, rng)
    plan: list[RunSpec] = []
    while True:
        _shuffle(order, rng)
        progressed = False
        for block in order:
            if block:
                plan.append(block.pop())
                progressed = True
        if not progressed:
            return plan


def build_run_plan(
    *,
    experiment_id: str,
    registry: CaseRegistry,
    conditions: ConditionsConfig,
    repeat_count: int,
    seed: int,
    models: ModelsConfig,
    condition_ids: Sequence[str] | None = None,
    case_ids: Sequence[str] | None = None,
) -> RunPlan:
    """Enumerate every job of one experiment, in a reproducibly randomized order.

    The full job set is built first, in a deterministic order, and only then
    shuffled. That separation is what makes "a different seed changes the order
    but not the membership" a structural fact rather than a property to be
    tested for and hoped for.
    """
    if not is_safe_component(experiment_id):
        raise RunPlanError(
            f"experiment_id {experiment_id!r} is not usable as a path segment"
        )
    if repeat_count < 1:
        raise RunPlanError(f"repeat_count must be at least 1, got {repeat_count}")

    selected_conditions = tuple(
        conditions.main_conditions if condition_ids is None else condition_ids
    )
    if not selected_conditions:
        raise RunPlanError("a run plan needs at least one governance condition")
    if len(set(selected_conditions)) != len(selected_conditions):
        raise RunPlanError(
            f"condition_ids contains duplicates: {list(selected_conditions)}"
        )
    for condition_id in selected_conditions:
        if not conditions.is_main(condition_id):
            raise RunPlanError(
                f"{condition_id!r} is not a permitted main experimental condition; "
                f"main conditions are {list(conditions.main_conditions)}"
            )

    selected_cases = tuple(registry.case_ids if case_ids is None else case_ids)
    if not selected_cases:
        raise RunPlanError("a run plan needs at least one case")
    for case_id in selected_cases:
        registry.get(case_id)

    fingerprint = model_fingerprint(models)

    # Deterministic pre-shuffle order: cases in registry order, error conditions
    # in enum order, conditions in configured order, repeats ascending. Sorted by
    # block key before shuffling so the RNG draw sequence does not depend on the
    # iteration order of a dict.
    blocks: dict[tuple[str, str], list[RunSpec]] = {}
    for case_id in selected_cases:
        case = registry.get(case_id)
        for error_condition in case.error_conditions:
            jobs: list[RunSpec] = []
            for condition_id in selected_conditions:
                condition = conditions.get(condition_id)
                for repeat_index in range(repeat_count):
                    jobs.append(
                        RunSpec(
                            run_id=make_run_id(
                                experiment_id=experiment_id,
                                case_id=case_id,
                                error_condition=error_condition,
                                condition_id=condition_id,
                                repeat_index=repeat_index,
                            ),
                            experiment_id=experiment_id,
                            case_id=case_id,
                            contract_id=case.contract_id,
                            condition_id=condition_id,
                            error_condition=error_condition,
                            source_access=condition.source_access,
                            verification_required=condition.verification_required,
                            repeat_index=repeat_index,
                            models_version=models.models_version,
                            model_fingerprint=fingerprint,
                        )
                    )
            blocks[(case_id, error_condition.value)] = jobs

    ordered_blocks = [blocks[key] for key in sorted(blocks)]
    plan = RunPlan(
        experiment_version=EXPERIMENT_VERSION,
        experiment_id=experiment_id,
        seed=seed,
        repeat_count=repeat_count,
        condition_ids=selected_conditions,
        case_ids=selected_cases,
        models_version=models.models_version,
        model_fingerprint=fingerprint,
        jobs=tuple(_interleave(ordered_blocks, random.Random(seed))),
    )
    plan.check(conditions, registry)
    return plan


def write_run_plan_csv(plan: RunPlan, path: str | Path) -> Path:
    """Write the plan as one tidy row per job.

    The plan is exported as CSV because it is the artifact an operator reads: a
    row per job, in the order the jobs will run, so the randomization can be
    inspected without loading the code that produced it. ``run_plan.json``
    alongside it is the artifact the code reads back -- the CSV is a projection
    and is not parsed again.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RUN_PLAN_COLUMNS))
        writer.writeheader()
        for position, job in enumerate(plan.jobs):
            writer.writerow(
                {
                    "position": position,
                    "run_id": job.run_id,
                    "experiment_id": job.experiment_id,
                    "case_id": job.case_id,
                    "contract_id": job.contract_id,
                    "condition_id": job.condition_id,
                    "error_condition": job.error_condition.value,
                    "source_access": job.source_access,
                    "verification_required": job.verification_required,
                    "repeat_index": job.repeat_index,
                    "models_version": job.models_version,
                    "model_fingerprint": job.model_fingerprint,
                }
            )
    return target


def iter_cells(plan: RunPlan) -> Iterable[tuple[str, str, str, int]]:
    """Every cell the plan occupies, in plan order."""
    return plan.cells()


__all__ += ["iter_cells"]

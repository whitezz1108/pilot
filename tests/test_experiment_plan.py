"""The run plan: membership, identity, and reproducible randomization.

Gate 3 §2. A plan is the frozen statement of *what will be run*, and every
property asserted here is one an archived experiment would be unreadable
without: every expected cell present exactly once, ids that are a function of
what a job is, an order that is reproducible from the seed alone, and a seed
that reorders the plan without changing its membership.
"""

from __future__ import annotations

import pytest

import fixture_registry as fixture
from pilot01.config import load_conditions_v1, load_models_v1
from pilot01.experiment import (
    CaseRegistry,
    RunPlan,
    RunPlanError,
    build_run_plan,
    make_run_id,
    model_fingerprint,
)
from pilot01.schemas import ErrorCondition


@pytest.fixture(scope="module")
def registry() -> CaseRegistry:
    return fixture.build_registry()


@pytest.fixture(scope="module")
def conditions():
    return load_conditions_v1()


@pytest.fixture(scope="module")
def models():
    return load_models_v1()


def plan_for(registry, conditions, models, **kwargs) -> RunPlan:
    kwargs.setdefault("experiment_id", "EXP-1")
    kwargs.setdefault("repeat_count", 1)
    kwargs.setdefault("seed", 0)
    return build_run_plan(registry=registry, conditions=conditions, models=models, **kwargs)


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


def test_every_expected_cell_appears_exactly_once(registry, conditions, models):
    plan = plan_for(registry, conditions, models, repeat_count=2)

    assert plan.missing_cells(registry) == ()
    assert plan.duplicate_cells() == ()
    assert len(plan.jobs) == len(set(plan.run_ids))

    expected = {
        (case_id, error.value, condition_id, repeat)
        for case_id in registry.case_ids
        for error in registry.get(case_id).error_conditions
        for condition_id in conditions.main_conditions
        for repeat in range(2)
    }
    assert set(plan.cells()) == expected


def test_a_sentinel_runs_only_the_arm_it_declares(registry, conditions, models):
    """CASE-C has no claim to omit, so it declares E0 alone and has no E1 cell.

    This is the case that makes "every case runs both arms" the wrong
    definition of completeness: a plan checked against that formula would
    report the sentinel's absent arm as a missing cell.
    """
    plan = plan_for(registry, conditions, models)
    sentinel = [job for job in plan.jobs if job.case_id == fixture.CASE_C]

    assert sentinel, "the sentinel should be planned"
    assert {job.error_condition for job in sentinel} == {ErrorCondition.E0}


def test_a_case_that_does_not_run_an_arm_cannot_be_planned_into_it(
    registry, conditions, models
):
    """The plan is checked against the registry, so an impossible job is refused.

    The forged job gets the run id its new identity *does* derive, so the id
    check cannot be what rejects it: the arm check is the one under test.
    """
    plan = plan_for(registry, conditions, models)
    job = next(j for j in plan.jobs if j.case_id == fixture.CASE_C)
    forged = job.model_copy(
        update={
            "error_condition": ErrorCondition.E1,
            "run_id": make_run_id(
                experiment_id=job.experiment_id,
                case_id=job.case_id,
                error_condition=ErrorCondition.E1,
                condition_id=job.condition_id,
                repeat_index=job.repeat_index,
            ),
        }
    )
    assert not registry.get(fixture.CASE_C).runs(ErrorCondition.E1)

    broken = plan.model_copy(update={"jobs": (forged,) + plan.jobs[1:]})
    with pytest.raises(RunPlanError, match="does not declare"):
        broken.check(conditions, registry)


def test_selecting_cases_and_conditions_narrows_the_plan(
    registry, conditions, models
):
    plan = plan_for(
        registry,
        conditions,
        models,
        case_ids=(fixture.CASE_A,),
        condition_ids=("A1V1",),
    )

    assert plan.case_ids == (fixture.CASE_A,)
    assert plan.condition_ids == ("A1V1",)
    assert {job.condition_id for job in plan.jobs} == {"A1V1"}
    assert {job.case_id for job in plan.jobs} == {fixture.CASE_A}


def test_a_plan_needs_at_least_one_case_and_one_condition(registry, conditions, models):
    with pytest.raises(RunPlanError):
        plan_for(registry, conditions, models, case_ids=())
    with pytest.raises(RunPlanError):
        plan_for(registry, conditions, models, condition_ids=())


def test_an_unknown_case_or_condition_is_refused(registry, conditions, models):
    with pytest.raises(Exception):
        plan_for(registry, conditions, models, case_ids=("CASE-NOPE",))
    with pytest.raises(RunPlanError):
        plan_for(registry, conditions, models, condition_ids=("A0V1",))


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_run_ids_are_a_function_of_the_job(registry, conditions, models):
    plan = plan_for(registry, conditions, models)
    for job in plan.jobs:
        assert job.run_id == make_run_id(
            experiment_id=job.experiment_id,
            case_id=job.case_id,
            error_condition=job.error_condition,
            condition_id=job.condition_id,
            repeat_index=job.repeat_index,
        )


def test_run_ids_are_unique_and_readable(registry, conditions, models):
    plan = plan_for(registry, conditions, models, repeat_count=2)
    for job in plan.jobs:
        assert job.run_id.count("__") == 4
        assert job.case_id in job.run_id
        assert job.condition_id in job.run_id
        assert job.error_condition.value in job.run_id


def test_a_run_id_carries_no_whitespace_or_path_separator(registry, conditions, models):
    plan = plan_for(registry, conditions, models)
    for job in plan.jobs:
        assert job.run_id == job.run_id.strip()
        assert "/" not in job.run_id and "\\" not in job.run_id
        assert ".." not in job.run_id


def test_an_unsafe_experiment_id_is_refused(registry, conditions, models):
    for bad in ("", "with space", "../escape", "a/b"):
        with pytest.raises(RunPlanError):
            plan_for(registry, conditions, models, experiment_id=bad)


def test_a_negative_repeat_index_is_refused():
    with pytest.raises(RunPlanError):
        make_run_id(
            experiment_id="EXP-1",
            case_id=fixture.CASE_A,
            error_condition=ErrorCondition.E0,
            condition_id="A0V0",
            repeat_index=-1,
        )


def test_the_plan_carries_the_model_fingerprint(registry, conditions, models):
    plan = plan_for(registry, conditions, models)
    assert plan.model_fingerprint == model_fingerprint(models)
    assert plan.models_version == models.models_version
    for job in plan.jobs:
        assert job.model_fingerprint == plan.model_fingerprint
        assert job.models_version == plan.models_version


# --------------------------------------------------------------------------
# Randomization
# --------------------------------------------------------------------------


def test_the_same_seed_reproduces_the_plan_exactly(registry, conditions, models):
    first = plan_for(registry, conditions, models, repeat_count=2, seed=11)
    second = plan_for(registry, conditions, models, repeat_count=2, seed=11)

    assert first.run_ids == second.run_ids
    assert first.fingerprint() == second.fingerprint()


def test_a_different_seed_reorders_without_changing_membership(
    registry, conditions, models
):
    base = plan_for(registry, conditions, models, repeat_count=2, seed=1)
    other = plan_for(registry, conditions, models, repeat_count=2, seed=2)

    assert base.run_ids != other.run_ids
    assert sorted(base.cells()) == sorted(other.cells())
    assert base.fingerprint() != other.fingerprint()


def test_the_order_is_stable_across_many_seeds(registry, conditions, models):
    """Every seed yields the same *set*, so membership is not a property of the seed."""
    reference = sorted(plan_for(registry, conditions, models, repeat_count=2).cells())
    for seed in (0, 1, 2, 3, 5, 8, 13, 21, 34, 55):
        plan = plan_for(registry, conditions, models, repeat_count=2, seed=seed)
        assert sorted(plan.cells()) == reference


def test_conditions_of_one_case_are_not_always_adjacent(registry, conditions, models):
    """A case's governance conditions must be spread through the batch.

    Concatenating cell by cell would place them together, which is the
    arrangement that lets a transient problem land on one case.
    """
    plan = plan_for(registry, conditions, models, repeat_count=2)
    positions: dict[tuple[str, str], list[int]] = {}
    for index, job in enumerate(plan.jobs):
        positions.setdefault(job.block, []).append(index)

    for block, indices in positions.items():
        span = max(indices) - min(indices)
        assert span > len(indices), (
            f"the jobs of block {block} occupy {indices}, which is not spread out"
        )


def test_a_condition_is_not_confined_to_one_stretch_of_the_batch(
    registry, conditions, models
):
    """The condition a job ran under must not be a function of when it ran.

    Dealing takes one job per block per round, so with a fixed within-block
    order every block would hand over the same condition in the same round and
    a provider that degraded halfway through would have degraded exactly one
    condition. Each condition's jobs must reach every part of the batch.
    """
    plan = plan_for(registry, conditions, models, repeat_count=2)
    total = len(plan.jobs)
    for condition_id in plan.condition_ids:
        positions = [
            index for index, job in enumerate(plan.jobs) if job.condition_id == condition_id
        ]
        assert len(positions) == total // len(plan.condition_ids)
        # Present in both halves of the batch, and not all in the same quarter.
        assert positions[0] < total // 2 <= positions[-1]
        quarters = {position * 4 // total for position in positions}
        assert len(quarters) >= 3, (
            f"{condition_id} occupies only quarter(s) {sorted(quarters)} of the batch"
        )


def test_randomization_does_not_touch_treatment_content(registry, conditions, models):
    """A seed changes the order and nothing else about a job."""
    base = {job.run_id: job for job in plan_for(registry, conditions, models, seed=1).jobs}
    other = {job.run_id: job for job in plan_for(registry, conditions, models, seed=99).jobs}

    assert base.keys() == other.keys()
    for run_id, job in base.items():
        assert job == other[run_id]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_a_plan_round_trips_through_json(registry, conditions, models, tmp_path):
    plan = plan_for(registry, conditions, models, repeat_count=2, seed=4)
    path = plan.write_json(tmp_path / "run_plan.json")

    restored = RunPlan.load_json(path)

    assert restored == plan
    assert restored.fingerprint() == plan.fingerprint()
    restored.check(conditions, registry)


def test_a_plan_from_another_format_version_is_refused(registry, conditions, models, tmp_path):
    plan = plan_for(registry, conditions, models)
    payload = plan.to_payload()
    payload["plan_version"] = "999"
    path = tmp_path / "run_plan.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")

    with pytest.raises(RunPlanError):
        RunPlan.load_json(path)


def test_a_missing_plan_file_is_reported(tmp_path):
    with pytest.raises(RunPlanError):
        RunPlan.load_json(tmp_path / "absent.json")


def test_the_csv_is_one_row_per_job_in_plan_order(registry, conditions, models, tmp_path):
    import csv

    from pilot01.experiment import write_run_plan_csv

    plan = plan_for(registry, conditions, models, repeat_count=2, seed=6)
    path = write_run_plan_csv(plan, tmp_path / "run_plan.csv")

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["run_id"] for row in rows] == list(plan.run_ids)
    assert [int(row["position"]) for row in rows] == list(range(len(plan.jobs)))
    assert {row["condition_id"] for row in rows} == set(plan.condition_ids)
    assert {row["case_id"] for row in rows} == set(plan.case_ids)

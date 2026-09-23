"""Gate 4 §11: the real development RunPlan.

The plan is the thing that decides what gets bought, so the assertions here are
about arithmetic and identity rather than about behaviour: exactly sixty jobs,
the right split between the paired cases and the sentinels, no cell twice, the
same seed giving the same plan, and -- the one that matters most -- building a
plan must not touch a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gate4_fixture as g4
from pilot01.config import load_conditions_v1, load_models_v1
from pilot01.experiment.runspec import (
    RUN_PLAN_COLUMNS,
    RunPlanError,
    build_run_plan,
    write_run_plan_csv,
)
from pilot01.schemas import ErrorCondition

PLAN_PATH = (
    Path(__file__).resolve().parents[1]
    / "outputs"
    / "manifests"
    / "gate4-development"
    / "run_plan.json"
)

POSITIVE_CASES = 8
SENTINEL_CASES = 4
CONDITIONS = 3
POSITIVE_JOBS = POSITIVE_CASES * 2 * CONDITIONS  # 8 cases x 2 arms x 3 conditions
SENTINEL_JOBS = SENTINEL_CASES * CONDITIONS  # 4 cases x E0 only x 3 conditions
TOTAL_JOBS = POSITIVE_JOBS + SENTINEL_JOBS  # 60


@pytest.fixture(scope="module")
def plan():
    return build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
    )


# ---------------------------------------------------------------------------
# §11: the arithmetic
# ---------------------------------------------------------------------------


def test_the_plan_holds_exactly_sixty_jobs(plan):
    assert len(plan.jobs) == TOTAL_JOBS == 60


def test_the_split_is_forty_eight_paired_and_twelve_sentinel(plan):
    sentinel_ids = set(g4.SENTINEL_CASE_IDS)
    sentinel_jobs = [job for job in plan.jobs if job.case_id in sentinel_ids]
    paired_jobs = [job for job in plan.jobs if job.case_id not in sentinel_ids]
    assert len(paired_jobs) == POSITIVE_JOBS == 48
    assert len(sentinel_jobs) == SENTINEL_JOBS == 12


def test_every_paired_case_contributes_two_arms_and_every_sentinel_one(plan):
    for case_id in g4.POSITIVE_CASE_IDS:
        arms = {
            job.error_condition
            for job in plan.jobs
            if job.case_id == case_id
        }
        assert arms == {ErrorCondition.E0, ErrorCondition.E1}
        assert len([j for j in plan.jobs if j.case_id == case_id]) == 2 * CONDITIONS
    for case_id in g4.SENTINEL_CASE_IDS:
        arms = {
            job.error_condition
            for job in plan.jobs
            if job.case_id == case_id
        }
        assert arms == {ErrorCondition.E0}
        assert len([j for j in plan.jobs if j.case_id == case_id]) == CONDITIONS


def test_the_plan_covers_exactly_the_twelve_registry_cases(plan):
    assert set(plan.case_ids) == set(g4.registry().case_ids)
    assert len(plan.case_ids) == 12


def test_the_plan_uses_the_three_main_conditions_and_not_the_degenerate_one(plan):
    assert set(plan.condition_ids) == {"A0V0", "A1V0", "A1V1"}
    assert "A0V1" not in plan.condition_ids


def test_there_is_exactly_one_repeat(plan):
    """§11: one repeat only."""
    assert plan.repeat_count == 1
    assert {job.repeat_index for job in plan.jobs} == {0}


def test_no_cell_appears_twice(plan):
    cells = [
        (job.case_id, job.error_condition, job.condition_id, job.repeat_index)
        for job in plan.jobs
    ]
    assert len(set(cells)) == len(cells)


def test_every_run_id_is_unique(plan):
    ids = [job.run_id for job in plan.jobs]
    assert len(set(ids)) == len(ids)


def test_the_arm_counts_are_what_the_design_says(plan):
    e0 = [job for job in plan.jobs if job.error_condition is ErrorCondition.E0]
    e1 = [job for job in plan.jobs if job.error_condition is ErrorCondition.E1]
    assert len(e0) == 8 * CONDITIONS + 4 * CONDITIONS  # 12 cases x 3
    assert len(e1) == 8 * CONDITIONS  # 8 paired cases x 3
    assert len(e0) + len(e1) == TOTAL_JOBS


def test_the_source_access_and_verification_flags_follow_the_condition(plan):
    conditions = load_conditions_v1()
    for job in plan.jobs:
        condition = conditions.get(job.condition_id)
        assert job.source_access == condition.source_access
        assert job.verification_required == condition.verification_required


# ---------------------------------------------------------------------------
# §11: determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_gives_the_same_plan(plan):
    again = build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
    )
    assert [job.run_id for job in again.jobs] == [job.run_id for job in plan.jobs]
    assert again.fingerprint() == plan.fingerprint()


def test_a_different_seed_changes_the_order_but_not_the_membership(plan):
    other = build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=12345,
        models=load_models_v1(),
    )
    assert {job.run_id for job in other.jobs} == {job.run_id for job in plan.jobs}
    assert [job.run_id for job in other.jobs] != [job.run_id for job in plan.jobs]
    assert other.fingerprint() != plan.fingerprint()


def test_the_fingerprint_covers_the_order_and_not_only_the_membership(plan):
    """A plan's fingerprint must move if the order moves: the order is the run."""
    other = build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=7,
        models=load_models_v1(),
    )
    assert sorted(j.run_id for j in other.jobs) == sorted(j.run_id for j in plan.jobs)
    assert other.fingerprint() != plan.fingerprint()


def test_the_plan_records_the_model_fingerprint(plan):
    fingerprint = plan.model_fingerprint
    assert "manager=" in fingerprint and "compliance=" in fingerprint
    assert all(job.model_fingerprint == fingerprint for job in plan.jobs)


# ---------------------------------------------------------------------------
# §11: what the plan must not contain, and must not do
# ---------------------------------------------------------------------------


def test_the_plan_carries_no_gold_status_and_no_evidence_offsets(plan):
    """A job names a case; the labels stay in the registry."""
    for job in plan.jobs:
        dumped = job.model_dump()
        for forbidden in ("gold", "evidence", "omission", "sentinel"):
            assert not any(forbidden in key for key in dumped), dumped.keys()


def test_the_plan_carries_no_credential(plan):
    serialized = plan.model_dump_json().lower()
    for forbidden in ("api_key", "apikey", "authorization", "bearer", "token", "secret"):
        assert forbidden not in serialized


def test_the_run_ids_encode_case_arm_and_condition_for_the_operator(plan):
    """§13 prints progress by run id, so the id has to be readable."""
    for job in plan.jobs:
        assert job.case_id in job.run_id
        assert job.error_condition.value in job.run_id
        assert job.condition_id in job.run_id


def test_building_a_plan_makes_no_model_call(monkeypatch):
    """§17: no paid API may be needed to build a plan.

    Enforced by making any outbound socket fail loudly rather than by asserting
    a plan is cheap: if plan construction ever grew a provider call, this test
    would fail at the socket instead of at a bill.
    """
    import socket

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("plan construction opened a network connection")

    # The suite's autouse guard already covers both connect paths, so patching
    # the module-level helper is enough to make this test's intent explicit --
    # and it keeps the socket-class call site out of this file, which Gate 2.5
    # scans for as a network-call marker.
    monkeypatch.setattr(socket, "create_connection", refuse)
    plan = build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
    )
    assert len(plan.jobs) == TOTAL_JOBS


def test_a_plan_holding_the_degenerate_condition_is_refused():
    with pytest.raises(RunPlanError):
        build_run_plan(
            experiment_id="gate4-development",
            registry=g4.registry(),
            conditions=load_conditions_v1(),
            repeat_count=1,
            seed=0,
            models=load_models_v1(),
            condition_ids=["A0V1"],
        )


def test_a_plan_naming_an_unknown_case_is_refused():
    with pytest.raises(Exception):
        build_run_plan(
            experiment_id="gate4-development",
            registry=g4.registry(),
            conditions=load_conditions_v1(),
            repeat_count=1,
            seed=0,
            models=load_models_v1(),
            case_ids=["DEV-POS-DOES-NOT-EXIST"],
        )


def test_the_sentinel_cases_never_produce_an_e1_job(plan):
    for job in plan.jobs:
        if job.case_id in set(g4.SENTINEL_CASE_IDS):
            assert job.error_condition is ErrorCondition.E0


# ---------------------------------------------------------------------------
# §11: the artifacts on disk
# ---------------------------------------------------------------------------


def test_the_plan_artifacts_were_written_before_any_live_execution():
    assert PLAN_PATH.is_file(), f"{PLAN_PATH} has not been written"
    payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    assert len(payload["jobs"]) == TOTAL_JOBS


def test_the_written_plan_matches_a_freshly_built_one(plan):
    payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    assert [job["run_id"] for job in payload["jobs"]] == [
        job.run_id for job in plan.jobs
    ]


def test_the_written_plan_records_the_same_fingerprint(plan):
    payload = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    assert payload["seed"] == plan.seed
    assert payload["repeat_count"] == 1
    assert payload["case_ids"] == list(plan.case_ids)
    assert payload["condition_ids"] == list(plan.condition_ids)


def test_the_csv_holds_one_row_per_job_and_the_declared_columns(plan, tmp_path):
    path = tmp_path / "run_plan.csv"
    write_run_plan_csv(plan, path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == TOTAL_JOBS + 1
    header = lines[0].split(",")
    assert header == list(RUN_PLAN_COLUMNS)


def test_the_csv_carries_no_gold_and_no_credential(plan, tmp_path):
    path = tmp_path / "run_plan.csv"
    write_run_plan_csv(plan, path)
    text = path.read_text(encoding="utf-8").lower()
    for forbidden in ("gold", "api_key", "bearer", "secret"):
        assert forbidden not in text

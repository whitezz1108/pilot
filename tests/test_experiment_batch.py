"""Executing a plan: order, identity, and the refusal to overwrite an observation.

Gate 3 §3 and §5. The batch runner's whole job is to make "which runs happened,
in what order, and what did they leave behind" answerable from the tree itself.
Every rule asserted here is one that, if it silently stopped holding, would
corrupt an experiment rather than raise.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest
import sample_case

import fixture_registry as fixture
from pilot01.experiment import (
    BatchError,
    ExperimentPaths,
    ExperimentRunner,
    JobStatus,
    ScriptedClientFactory,
    load_response_script,
    plan_or_raise,
    read_raw_run,
)
from pilot01.experiment.artifacts import MODEL_CALLS_JSONL, PARTIAL_SUFFIX
from pilot01.schemas import ClauseStatus, Decision, VerificationStatus


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry():
    return _registry()


def narrow_plan(**kwargs):
    kwargs.setdefault("case_ids", (fixture.CASE_A, fixture.CASE_B))
    kwargs.setdefault("condition_ids", ("A1V1", "A0V0"))
    return fixture.make_plan(registry=_registry(), **kwargs)


def paths_for(tmp_path: Path) -> ExperimentPaths:
    return ExperimentPaths(root=tmp_path, experiment_id="EXP-1")


def always_fails(job, registry):
    """A script whose answers are not JSON, so the run dies at the Manager."""
    return {"manager": ("no object here", "still none"), "compliance": ("unused",)}


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def test_every_job_gets_a_directory_and_a_completed_run(tmp_path):
    plan = narrow_plan()
    paths = paths_for(tmp_path)

    batch = fixture.execute(plan, _registry(), paths)

    assert len(batch.results) == len(plan.jobs)
    assert batch.completed and len(batch.completed) == len(plan.jobs)
    for result in batch.results:
        assert Path(result.directory).is_dir()
        assert read_raw_run(result.directory).artifact.run_id == result.run_id


def test_runs_happen_in_plan_order(tmp_path):
    """The plan's order *is* the randomization, so the runner must not reorder it."""
    plan = narrow_plan()
    paths = paths_for(tmp_path)
    seen: list[str] = []

    runner = ExperimentRunner(
        plan=plan,
        registry=_registry(),
        conditions=fixture.conditions(),
        workflow=fixture.workflow(),
        models=fixture.models(),
        client_factory=ScriptedClientFactory(fixture.script_for_plan(plan, _registry())),
        paths=paths,
        progress=lambda position, total, result: seen.append(result.run_id),
    )
    runner.run()

    assert seen == list(plan.run_ids)


def test_each_run_gets_its_own_call_sequence(tmp_path):
    """A transport reused across runs would carry its cursor into the next run.

    ``call_index`` is dense within one node's invocation, not within the run --
    the Manager's turns and Compliance's turns each start at zero, because each
    invocation is its own conversation. What must hold across the whole run is
    that no two calls share an address, and that no run's log holds another
    run's calls.
    """
    plan = narrow_plan()
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths)

    for raw in (read_raw_run(paths.run_dir(job.run_id)) for job in plan.jobs):
        addresses = [(record.role, record.invocation, record.call_index) for record in raw.model_calls]
        assert len(addresses) == len(set(addresses))
        for role, invocation in {(role, inv) for role, inv, _ in addresses}:
            indices = [
                index
                for r, i, index in addresses
                if (r, i) == (role, invocation)
            ]
            assert indices == list(range(len(indices)))
        assert {record.run_id for record in raw.model_calls} == {raw.run_id}
        # The tool log is run-wide, so its sequence is dense across both nodes.
        assert [record.sequence for record in raw.tool_calls] == list(
            range(len(raw.tool_calls))
        )


def test_the_run_id_reaches_the_logs_but_never_a_prompt(tmp_path):
    """A run id is researcher-facing; a model request must not carry it."""
    plan = narrow_plan()
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths)

    for job in plan.jobs:
        raw = read_raw_run(paths.run_dir(job.run_id))
        assert raw.model_calls
        for record in raw.model_calls:
            assert job.run_id not in record.rendered_input
            assert job.condition_id not in record.rendered_input
            assert job.error_condition.value not in record.rendered_input


def test_the_batch_result_is_written_and_reads_back(tmp_path):
    plan = narrow_plan()
    paths = paths_for(tmp_path)
    batch = fixture.execute(plan, _registry(), paths)

    path = batch.write_json(paths.batch_index_json)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["experiment_id"] == plan.experiment_id
    assert payload["plan_fingerprint"] == plan.fingerprint()
    assert [row["run_id"] for row in payload["results"]] == list(plan.run_ids)
    assert batch.counts == {"completed": len(plan.jobs)}


def test_a_failed_run_is_still_an_observation(tmp_path):
    """§7: a protocol failure is not a missing row."""
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)

    batch = fixture.execute(plan, _registry(), paths, behaviour=always_fails)

    assert len(batch.results) == len(plan.jobs)
    assert len(batch.failed) == len(plan.jobs)
    for result in batch.results:
        assert result.produced_observation
        assert result.failure is not None and result.failure.protocol_failure
        raw = read_raw_run(result.directory)
        assert raw.artifact.failed
        assert not raw.artifact.completed


def test_skipped_is_not_the_same_finding_as_failed(tmp_path):
    plan = narrow_plan()
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths)

    again = fixture.execute(plan, _registry(), paths, resume=True)

    assert len(again.skipped) == len(plan.jobs)
    assert again.failed == ()
    assert all(not result.produced_observation for result in again.skipped)
    assert all("already complete" in (result.detail or "") for result in again.skipped)


# --------------------------------------------------------------------------
# Never overwriting
# --------------------------------------------------------------------------


def test_a_second_run_without_resume_refuses_to_overwrite(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths)

    with pytest.raises(BatchError, match="Refusing to overwrite"):
        fixture.execute(plan, _registry(), paths)


def test_rerun_failed_requires_resume(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)

    with pytest.raises(BatchError, match="rerun_failed requires resume"):
        fixture.execute(plan, _registry(), paths, rerun_failed=True)


def test_resume_rerun_failed_replaces_a_failed_attempt(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths, behaviour=always_fails)
    assert all(paths.run_dir(job.run_id).is_dir() for job in plan.jobs)

    again = fixture.execute(plan, _registry(), paths, resume=True, rerun_failed=True)

    assert len(again.rerun) == len(plan.jobs)
    assert all(result.status is JobStatus.RERUN for result in again.results)
    for job in plan.jobs:
        assert read_raw_run(paths.run_dir(job.run_id)).artifact.completed
        # The replaced attempt is kept, not deleted: "the run died here" is worth
        # being able to look at.
        assert (paths.raw_dir / f"{job.run_id}{PARTIAL_SUFFIX}").is_dir()


def test_resume_rerun_failed_leaves_a_completed_run_alone(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)
    fixture.execute(plan, _registry(), paths)

    again = fixture.execute(plan, _registry(), paths, resume=True, rerun_failed=True)

    assert len(again.skipped) == len(plan.jobs)
    assert again.rerun == ()


def test_an_interrupted_run_is_refused_without_resume(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)
    job = plan.jobs[0]
    directory = paths.run_dir(job.run_id)
    directory.mkdir(parents=True)
    (directory / MODEL_CALLS_JSONL).write_text("half a run\n", encoding="utf-8")

    with pytest.raises(BatchError, match="interrupted run"):
        fixture.execute(plan, _registry(), paths)


def test_resume_moves_an_interrupted_run_aside_and_reruns_it(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)
    job = plan.jobs[0]
    directory = paths.run_dir(job.run_id)
    directory.mkdir(parents=True)
    (directory / MODEL_CALLS_JSONL).write_text("half a run\n", encoding="utf-8")

    batch = fixture.execute(plan, _registry(), paths, resume=True)

    moved = paths.raw_dir / f"{job.run_id}{PARTIAL_SUFFIX}"
    assert moved.is_dir()
    assert (moved / MODEL_CALLS_JSONL).read_text(encoding="utf-8") == "half a run\n"
    # The fresh attempt starts its own sequence rather than appending to the dead
    # one's call indices.
    raw = read_raw_run(directory)
    assert raw.model_calls
    assert [record.call_index for record in raw.model_calls if record.role == "manager"] == [
        0,
        1,
        2,
    ]
    assert "half a run" not in (directory / MODEL_CALLS_JSONL).read_text(encoding="utf-8")
    detail = next(r for r in batch.results if r.run_id == job.run_id).detail
    assert detail and PARTIAL_SUFFIX in detail


# --------------------------------------------------------------------------
# fail_fast
# --------------------------------------------------------------------------


def test_fail_fast_stops_at_the_first_failure(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)

    batch = fixture.execute(plan, _registry(), paths, behaviour=always_fails, fail_fast=True)

    assert len(batch.results) == 1
    assert batch.results[0].status is JobStatus.FAILED
    assert len(batch.results) < len(plan.jobs)


def test_without_fail_fast_every_job_is_attempted(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)

    batch = fixture.execute(plan, _registry(), paths, behaviour=always_fails)

    assert len(batch.results) == len(plan.jobs)


# --------------------------------------------------------------------------
# The plan is re-checked before anything runs
# --------------------------------------------------------------------------


def test_a_plan_that_disagrees_with_the_registry_is_refused_before_running(tmp_path):
    """A plan read back from disk is an input like any other."""
    plan = narrow_plan()
    other = fixture.registry_for((fixture.CASE_C,))
    paths = paths_for(tmp_path)

    runner = ExperimentRunner(
        plan=plan,
        registry=other,
        conditions=fixture.conditions(),
        workflow=fixture.workflow(),
        models=fixture.models(),
        client_factory=ScriptedClientFactory(fixture.script_for_plan(plan, _registry())),
        paths=paths,
    )

    with pytest.raises(Exception):
        runner.run()
    assert not paths.raw_dir.exists() or not list(paths.raw_dir.iterdir())


def test_plan_or_raise_translates_a_read_failure(tmp_path):
    with pytest.raises(BatchError):
        plan_or_raise(tmp_path / "absent.json")


def test_plan_or_raise_loads_a_written_plan(tmp_path):
    plan = narrow_plan()
    path = plan.write_json(tmp_path / "run_plan.json")

    assert plan_or_raise(path) == plan


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


def test_a_scripted_factory_refuses_a_run_it_does_not_declare(tmp_path):
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    script = fixture.script_for_plan(plan, _registry())
    script.pop(plan.jobs[0].run_id)
    paths = paths_for(tmp_path)

    with pytest.raises(BatchError, match="declares no responses for run"):
        fixture.execute(
            plan, _registry(), paths, factory=ScriptedClientFactory(script)
        )


def test_a_scripted_factory_refuses_an_undeclared_role():
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    script = {job.run_id: {"manager": ("x",)} for job in plan.jobs}
    factory = ScriptedClientFactory(script)

    with pytest.raises(BatchError, match="declares no responses for role"):
        factory(role="compliance", job=plan.jobs[0])


def test_load_response_script_reads_the_declared_shape(tmp_path):
    path = tmp_path / "responses.json"
    path.write_text(json.dumps({"run-1": {"manager": ["a", "b"]}}), encoding="utf-8")

    script = load_response_script(path)

    assert script == {"run-1": {"manager": ("a", "b")}}


@pytest.mark.parametrize(
    "payload",
    [
        {"run-1": {"manager": "a single string"}},
        {"run-1": {"manager": []}},
        {"run-1": {}},
        {"run-1": {"manager": [1, 2]}},
        [],
    ],
)
def test_load_response_script_refuses_a_malformed_script(tmp_path, payload):
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BatchError):
        load_response_script(path)


def test_load_response_script_reports_a_missing_or_broken_file(tmp_path):
    with pytest.raises(BatchError, match="no response script"):
        load_response_script(tmp_path / "absent.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(BatchError, match="not valid JSON"):
        load_response_script(broken)


def test_a_transport_error_fails_the_run_rather_than_the_batch(tmp_path):
    """A run whose script runs out is a failed observation, not a crashed batch."""
    plan = narrow_plan(case_ids=(fixture.CASE_A,), condition_ids=("A1V1",))
    paths = paths_for(tmp_path)

    def one_answer_only(job, registry):
        text = sample_case.manager_response_text(
            clause_status=ClauseStatus.PRESENT,
            decision=Decision.ESCALATE,
            evidence_ids=(fixture.TARGET_PARAGRAPH_ID,),
            verification_status=VerificationStatus.VERIFIED,
        )
        return {"manager": (text,), "compliance": (text,)}

    batch = fixture.execute(plan, _registry(), paths, behaviour=one_answer_only)

    assert len(batch.results) == len(plan.jobs)
    assert all(result.status is JobStatus.FAILED for result in batch.results)
    assert all(result.failure is not None for result in batch.results)

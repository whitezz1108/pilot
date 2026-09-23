"""Raw run artifacts: what a run leaves behind, and how it is read back.

Gate 3 §4 and §6. The claim under test is that a raw tree is the *record*: a run
that finished is distinguishable from one that died without consulting any
message text, and the classification survives being re-derived from disk by code
that never saw the run.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest
import sample_case

import fixture_registry as fixture
from pilot01.experiment import (
    ArtifactError,
    ExperimentPaths,
    JobStatus,
    RunArtifact,
    RunDirectory,
    derive_failure,
    iter_raw_runs,
    read_raw_run,
)
from pilot01.experiment.artifacts import (
    EVENTS_JSONL,
    LOG_FILES,
    MODEL_CALLS_JSONL,
    PARTIAL_SUFFIX,
    RUN_JSON,
    TOOL_CALLS_JSONL,
    VERIFICATION_JSONL,
)
from pilot01.schemas import ClauseStatus, Decision, VerificationStatus


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry():
    return _registry()


def run_plan(
    tmp_path: Path, *, case_ids=(fixture.CASE_A,), condition_ids=("A1V1",), behaviour=None
):
    """Run a narrow plan and return its paths, its batch result, and the plan."""
    registry = _registry()
    plan = fixture.make_plan(
        registry=registry, case_ids=case_ids, condition_ids=condition_ids
    )
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    batch = fixture.execute(plan, registry, paths, behaviour=behaviour)
    return paths, batch, plan


def run_one(tmp_path: Path, **kwargs):
    """Run a narrow plan and return the *first* job's directory, the batch, and the job.

    The first job in plan order, not a named one: the plan is randomized, so
    which arm of ``CASE-A`` comes first is a property of the seed. Tests that
    need a specific arm select it from the batch rather than assuming a position.
    """
    paths, batch, plan = run_plan(tmp_path, **kwargs)
    job = plan.jobs[0]
    return paths.run_dir(job.run_id), batch, job


# --------------------------------------------------------------------------
# The file set
# --------------------------------------------------------------------------


def test_a_completed_run_leaves_exactly_the_five_expected_files(tmp_path):
    directory, _, _ = run_one(tmp_path)

    names = sorted(child.name for child in directory.iterdir())
    assert names == sorted([RUN_JSON, *LOG_FILES])


def test_run_json_is_written_and_no_temporary_is_left_behind(tmp_path):
    """The atomic write goes through ``.tmp`` and ``os.replace``, so no partial remains."""
    directory, _, _ = run_one(tmp_path)

    assert (directory / RUN_JSON).is_file()
    assert list(directory.glob("*.tmp")) == []


def test_run_json_is_the_definition_of_a_complete_run(tmp_path):
    directory, _, _ = run_one(tmp_path)
    layout = RunDirectory(directory)

    assert layout.is_complete()

    (directory / RUN_JSON).unlink()
    assert not layout.is_complete()


def test_the_logs_are_jsonl_with_one_record_per_line(tmp_path):
    directory, _, _ = run_one(tmp_path)

    for name in (MODEL_CALLS_JSONL, TOOL_CALLS_JSONL, VERIFICATION_JSONL, EVENTS_JSONL):
        text = (directory / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                assert isinstance(json.loads(line), dict), name


def test_a_run_that_dies_mid_flight_still_leaves_the_calls_it_made(tmp_path):
    """The call log is bound to its file up front and appended live."""
    directory, batch, job = run_one(
        tmp_path,
        behaviour=lambda job, registry: {
            "manager": ("not a JSON object at all", "still not one"),
            "compliance": ("unused",),
        },
    )

    assert (directory / MODEL_CALLS_JSONL).is_file()
    assert (directory / MODEL_CALLS_JSONL).read_text(encoding="utf-8").strip()
    assert batch.results[0].status is JobStatus.FAILED


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


def test_a_raw_run_reads_back_whole(tmp_path):
    directory, _, job = run_one(tmp_path)
    raw = read_raw_run(directory)

    assert raw.run_id == job.run_id
    assert raw.artifact.case_id == fixture.CASE_A
    assert raw.artifact.condition_id == "A1V1"
    assert raw.artifact.completed
    assert raw.model_calls and raw.tool_calls and raw.verifications and raw.events
    assert raw.model_calls_of("manager")
    assert raw.tool_calls_of("manager")
    assert raw.verifications_of("manager")
    assert len(raw.model_calls_of("manager")) + len(
        raw.model_calls_of("compliance")
    ) == len(raw.model_calls)


def test_reading_an_interrupted_run_is_refused(tmp_path):
    """A directory without ``run.json`` is not an observation."""
    directory, _, _ = run_one(tmp_path)
    (directory / RUN_JSON).unlink()

    with pytest.raises(ArtifactError, match="interrupted run"):
        read_raw_run(directory)


def test_reading_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(ArtifactError):
        read_raw_run(tmp_path / "nope")


def test_iter_raw_runs_skips_partials_and_sorts(tmp_path):
    paths, _, plan = run_plan(
        tmp_path, case_ids=(fixture.CASE_A, fixture.CASE_B), condition_ids=("A1V1",)
    )

    # A killed attempt: a directory with logs but no run.json.
    partial = paths.raw_dir / "EXP-1__CASE-A__E1__A1V1__r000.partial"
    partial.mkdir(parents=True)
    (partial / MODEL_CALLS_JSONL).write_text("", encoding="utf-8")

    run_ids = [raw.run_id for raw in iter_raw_runs(paths.raw_dir)]

    assert run_ids == sorted(run_ids)
    assert len(run_ids) == len(plan.jobs)
    assert not any(run_id.endswith(PARTIAL_SUFFIX) for run_id in run_ids)


def test_iter_raw_runs_over_a_missing_root_yields_nothing(tmp_path):
    assert list(iter_raw_runs(tmp_path / "absent")) == []


def test_move_aside_renames_and_replaces_an_older_partial(tmp_path):
    directory = tmp_path / "run"
    layout = RunDirectory(directory)
    layout.prepare()
    (directory / MODEL_CALLS_JSONL).write_text("first attempt\n", encoding="utf-8")

    moved = layout.move_aside()
    assert moved is not None and moved.name == f"run{PARTIAL_SUFFIX}"
    assert (moved / MODEL_CALLS_JSONL).read_text(encoding="utf-8") == "first attempt\n"
    assert not directory.exists()

    # A second interrupted attempt replaces the first: two partials of the same
    # run are never both useful, and keeping both would make the newer one
    # unreachable under the name the reader looks for.
    layout.prepare()
    (directory / MODEL_CALLS_JSONL).write_text("second attempt\n", encoding="utf-8")
    layout.move_aside()

    assert (moved / MODEL_CALLS_JSONL).read_text(encoding="utf-8") == "second attempt\n"


def test_move_aside_on_an_absent_directory_does_nothing(tmp_path):
    assert RunDirectory(tmp_path / "absent").move_aside() is None


# --------------------------------------------------------------------------
# Failure classification, derived from typed artifacts
# --------------------------------------------------------------------------


def test_a_completed_run_reports_no_failure(tmp_path):
    directory, _, _ = run_one(tmp_path)
    raw = read_raw_run(directory)

    assert raw.artifact.failure is not None
    assert raw.artifact.failure.protocol_failure is False
    assert raw.artifact.failure.verification_failure is False
    assert raw.artifact.failure.tool_failure is False
    assert raw.artifact.failure.model_output_failure is False
    assert raw.artifact.failed is False
    assert raw.artifact.status.value == "completed"


def test_a_verification_failure_is_classified_from_the_verdicts(tmp_path):
    """A1V1 with an answer that used no tool: the verdict says so, not the message."""

    def answer_without_tools(job, registry):
        text = sample_case.manager_response_text(
            clause_status=ClauseStatus.PRESENT,
            decision=Decision.ESCALATE,
            evidence_ids=(fixture.TARGET_PARAGRAPH_ID,),
            verification_status=VerificationStatus.VERIFIED,
        )
        return {"manager": (text,), "compliance": (text,)}

    directory, batch, _ = run_one(tmp_path, behaviour=answer_without_tools)
    raw = read_raw_run(directory)

    assert batch.results[0].status is JobStatus.FAILED
    failure = raw.artifact.failure
    assert failure.protocol_failure is True
    assert failure.failure_type == "ProtocolError"
    assert failure.failure_stage == "manager"
    assert failure.verification_failure is True
    assert "no_tool_use" in failure.verification_failures
    # The verdict explains it, so it is not a model-output failure.
    assert failure.model_output_failure is False
    # A failed run is still an observation: the artifact exists and is readable.
    assert raw.artifact.failed is True
    assert raw.artifact.completed is False
    assert raw.artifact.final_decision() is None


def test_a_refused_tool_is_recorded_without_failing_the_run(tmp_path):
    """A0V0 offers no tools; asking for one is refused, logged, and survivable."""
    paragraph = fixture.OPENABLE_PARAGRAPH[fixture.CASE_A]

    def asks_for_a_tool_then_answers(job, registry):
        # The default script's own answers, with a tool request prepended to each
        # role's turns. Building it this way rather than writing fresh answers
        # keeps the adopted claim ids correct for the arm: a node that adopts a
        # claim the handoff never carried is refused by its own output schema,
        # which would turn this into a test about adoption.
        base = fixture.default_responses(job, registry)
        return {
            "manager": (
                sample_case.tool_request_text("search_contract", query="change of control"),
                *base["manager"],
            ),
            "compliance": (
                sample_case.tool_request_text("open_source_span", paragraph_id=paragraph),
                *base["compliance"],
            ),
        }

    directory, batch, _ = run_one(
        tmp_path, condition_ids=("A0V0",), behaviour=asks_for_a_tool_then_answers
    )
    raw = read_raw_run(directory)

    assert batch.results[0].status is JobStatus.COMPLETED
    failure = raw.artifact.failure
    assert failure.protocol_failure is False
    assert failure.tool_failure is True
    assert failure.tool_failures == 2
    assert all(not record.ok for record in raw.tool_calls)
    assert all(record.available is False for record in raw.tool_calls)


def test_a_model_that_never_produces_an_object_is_a_model_output_failure(tmp_path):
    directory, batch, _ = run_one(
        tmp_path,
        behaviour=lambda job, registry: {
            "manager": ("I am unable to answer.", "I still cannot answer."),
            "compliance": ("unused",),
        },
    )
    raw = read_raw_run(directory)
    failure = raw.artifact.failure

    assert batch.results[0].status is JobStatus.FAILED
    assert failure.protocol_failure is True
    assert failure.model_output_failure is True
    assert failure.verification_failure is False
    assert any(record.parse_error for record in raw.model_calls)


def test_derive_failure_reads_records_not_messages(tmp_path):
    """The classifier's inputs are typed; a reworded message changes nothing."""
    directory, _, _ = run_one(tmp_path)
    raw = read_raw_run(directory)

    again = derive_failure(
        events=raw.events,
        model_calls=raw.model_calls,
        tool_calls=raw.tool_calls,
        verifications=raw.verifications,
    )

    assert again == raw.artifact.failure


def test_derive_failure_with_no_evidence_at_all_is_a_clean_record():
    failure = derive_failure(events=(), model_calls=(), tool_calls=(), verifications=())
    assert failure.protocol_failure is False
    assert failure.failure_type is None
    assert failure.model_output_failure is False


# --------------------------------------------------------------------------
# The artifact envelope
# --------------------------------------------------------------------------


def test_the_artifact_round_trips_through_json(tmp_path):
    directory, _, _ = run_one(tmp_path)
    payload = json.loads((directory / RUN_JSON).read_text(encoding="utf-8"))

    artifact = RunArtifact.from_payload(payload)

    assert artifact.run_id == read_raw_run(directory).run_id
    assert artifact == read_raw_run(directory).artifact


def test_an_artifact_from_another_version_is_refused(tmp_path):
    directory, _, _ = run_one(tmp_path)
    payload = json.loads((directory / RUN_JSON).read_text(encoding="utf-8"))
    payload["artifact_version"] = "999"

    with pytest.raises(ArtifactError, match="artifact_version"):
        RunArtifact.from_payload(payload)


def test_an_artifact_refuses_a_gold_shaped_key(tmp_path):
    """Gold belongs to the registry and the scorer, never to a raw run file."""
    directory, _, _ = run_one(tmp_path)
    payload = json.loads((directory / RUN_JSON).read_text(encoding="utf-8"))
    payload["gold_clause_status"] = "present"

    with pytest.raises(Exception, match="gold"):
        RunArtifact.from_payload(payload)


def test_an_artifact_refuses_to_disagree_with_its_execution_record(tmp_path):
    directory, _, _ = run_one(tmp_path)
    payload = json.loads((directory / RUN_JSON).read_text(encoding="utf-8"))
    payload["condition_id"] = "A0V0"

    with pytest.raises(ArtifactError, match="condition"):
        RunArtifact.from_payload(payload)


def test_an_artifact_refuses_a_mislabelled_arm(tmp_path):
    """Flipping the arm on the envelope must not survive the consistency check."""
    directory, _, _ = run_one(tmp_path)
    payload = json.loads((directory / RUN_JSON).read_text(encoding="utf-8"))
    other = "E1" if payload["error_condition"] == "E0" else "E0"
    payload["error_condition"] = other

    with pytest.raises(ArtifactError, match="labelled"):
        RunArtifact.from_payload(payload)

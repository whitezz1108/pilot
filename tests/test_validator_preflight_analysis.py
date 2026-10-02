"""Report delivery denominators without turning rejected runs into wrong answers."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest
import fixture_registry as fixture
import sample_case
from pilot01.experiment.layout import ExperimentPaths


def analyzer():
    folder = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location("analyze_validator_preflight", folder / "analyze_validator_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_run(tmp_path):
    registry = fixture.build_registry()
    path = registry.write_json(tmp_path / "registry.json")
    plan = fixture.make_plan(registry=registry, case_ids=(fixture.CASE_A,),
                             condition_ids=("A0V0", "A1V0", "A1V1"))
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id).ensure()
    plan.write_json(paths.run_plan_json)
    (tmp_path / "validator_manifest.json").write_text(json.dumps({
        "validator_version": "validator_v2_1", "implementation_commit": "test-only",
        "sha256": {},
    }), encoding="utf-8")
    def responses(job, registry):
        if job.verification_required:
            return {"manager": (sample_case.manager_response_text(),),
                    "compliance": (sample_case.compliance_response_text(),)}
        return fixture.default_responses(job, registry)
    fixture.execute(plan, registry, paths, behaviour=responses)
    return paths, path


def test_rejected_runs_remain_in_delivery_denominator_but_not_answer_accuracy(tmp_path):
    paths, registry_path = setup_run(tmp_path)
    result = analyzer().analyze(tmp_path, paths.experiment_id, registry_path)
    required = next(row for row in result["conditions"] if row["condition_id"] == "A1V1")
    assert result["qc"]["planned"] == result["qc"]["attempted"] == 6
    assert result["qc"]["completed"] == 4 and result["qc"]["failed"] == 2
    assert result["qc"]["protocol_invariants_pass"]
    assert required["action_correct_among_answers"] == {"numerator": 0, "denominator": 0}
    assert required["correct_action_delivered_over_planned"]["denominator"] == 2
    assert required["e1_recovery_among_completed"]["denominator"] == 0
    assert required["e1_recovery_delivered_over_planned"]["denominator"] == 1
    assert required["manager_full_validation"] == {"numerator": 0, "denominator": 2}
    assert required["compliance_full_validation"] == {"numerator": 0, "denominator": 0}
    assert (tmp_path / "raw_sha256.json").is_file()
    assert paths.run_scores_csv.is_file()


def test_incomplete_tree_is_rejected_before_reporting(tmp_path):
    paths, registry_path = setup_run(tmp_path)
    first = next(paths.raw_dir.glob("*/run.json"))
    first.unlink()  # Synthetic test directory only.
    with pytest.raises(ValueError, match="Incomplete raw tree"):
        analyzer().analyze(tmp_path, paths.experiment_id, registry_path)

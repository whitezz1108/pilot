"""Save a synthetic six-cell run and paging probe. Never constructs a live client."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from clean_v2_fixture import materials, profile, script, TARGETS
from pilot01.experiment.runspec import build_run_plan
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.scoring_v2 import write_report
from pilot01.source.pagination import PagedSourceTools


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    out = Path(args.out)
    if out.exists():
        p.error("choose a new output path; validation records are not overwritten")
    _, registry, _ = materials()
    config = profile()
    plan = build_run_plan(experiment_id="synthetic-clean-v2", registry=registry, conditions=config.conditions,
        repeat_count=1, seed=20261017, models=config.models, runtime_config=config.runtime,
        profile_fingerprint=config.fingerprint())
    paths = ExperimentPaths(root=out, experiment_id=plan.experiment_id).ensure()
    plan.write_json(paths.run_plan_json)
    registry.write_json(out / "synthetic_registry.json")
    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    responses = {j.run_id: {role: script(pid) for role in ("manager", "compliance")} for j in plan.jobs}
    (out / "synthetic_responses.json").write_text(json.dumps(responses, indent=2), encoding="utf-8")
    runner = ExperimentRunner(plan=plan, registry=registry, conditions=config.conditions, workflow=config.workflow,
        models=config.models, client_factory=ScriptedClientFactory(responses), paths=paths, profile=config)
    batch = runner.run()
    batch.write_json(paths.batch_index_json)
    summary = write_report(plan, registry, paths.raw_dir, paths.processed_dir)
    tools = PagedSourceTools(node="paging-probe", library=registry.library, target_categories=TARGETS)
    tools.begin_invocation(contract_id="CONTRACT-001", available=True)
    first = tools.search_contract("administrative", target_category=TARGETS[0])
    second = tools.search_contract("administrative", target_category=TARGETS[0], cursor=first.payload["next_cursor"])
    repeat = tools.search_contract("administrative", target_category=TARGETS[0])
    assert set(first.result_ids).isdisjoint(second.result_ids) and repeat.payload["repeat_page"]
    (out / "paging_probe.json").write_text(json.dumps([x.model_dump(mode="json") for x in (first, second, repeat)], indent=2), encoding="utf-8")
    result = {"kind": "synthetic scripted validation, not CUAD experiment results", "network_calls": 0,
              "cells": len(plan.jobs), "completed": summary["overall"]["execution_statuses"].get("completed", 0),
              "tools_per_node": config.runtime.max_tool_requests_per_node,
              "max_output_tokens": config.models.for_role("manager").max_output_tokens,
              "timeout_seconds": config.models.for_role("manager").timeout_seconds,
              "paging_disjoint": True, "repeat_first_page_suppressed": True}
    (out / "validation_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result["completed"] != result["cells"]:
        raise RuntimeError("synthetic workflow validation failed")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

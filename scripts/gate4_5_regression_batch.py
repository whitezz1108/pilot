"""A small, offline regression batch over the revised pipeline.

Not a research run and not a result. It exists to answer one question after a
prompt, schema or policy revision: *does the whole pipeline still work end to
end, and do the §14 metrics still compute?* The development batch answers that
for the revision it ran under; this answers it for the current one, at the price
of a handful of scripted runs rather than sixty live ones.

What it does, in order:

1. builds the development set at the revision named (revision 2 by default),
   offline, from the same modules the build script uses;
2. takes a small slice of the grid -- one positive case and one negative
   sentinel, across all six cells -- so both arms and all three governance
   conditions appear;
3. writes the plan and a **declared** response script beside it, so the batch is
   reproducible without a key and without a network;
4. runs it through the real :class:`~pilot01.experiment.batch.ExperimentRunner`
   and scores it with the real scorer;
5. writes the scores and a short report under a revision-suffixed directory.

The declared behaviour is the least interesting one available: each node
searches, opens the paragraph the case's gold evidence falls in, and answers with
the case's gold status and action. It is a plumbing check, so it declares the
answer rather than observing one -- a node that got the answer right here means
the pipeline carried what it was handed, not that a model did well.

Nothing here is written over: every artifact, including the raw tree, lands
under ``outputs/development/regression_v<revision>/``. The two revisions can
therefore be run under the same ``--out`` root.

    .venv/Scripts/python.exe scripts/gate4_5_regression_batch.py
    .venv/Scripts/python.exe scripts/gate4_5_regression_batch.py --policy-version 1

Exit status is 0 when the batch completes and scores, and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from pilot01.config import load_conditions_v1, load_models_v1, load_workflow_v1  # noqa: E402
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory  # noqa: E402
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.runspec import build_run_plan  # noqa: E402
from pilot01.experiment.scoring import score_experiment  # noqa: E402
from pilot01.experiment.summary import summarise  # noqa: E402
from pilot01.schemas import ErrorCondition  # noqa: E402
from pilot01.source.tools import OPEN_SOURCE_SPAN, SEARCH_CONTRACT  # noqa: E402
from pilot01.workflow.nodes import FrozenMemoRepository  # noqa: E402

from build_development_cases import (  # noqa: E402
    DEFAULT_POLICY_VERSION,
    POLICY_LOADERS,
    build_gate4_development_set,
)

EXPERIMENT_ID = "gate5-regression"
"""Its own experiment id, so a regression batch can never be read as the batch."""

SMALL_SLICE = (1, 1)
"""One positive case, one sentinel. Enough for both arms and every cell."""


def _tool_request(tool: str, **arguments: str) -> str:
    return json.dumps({"tool_request": {"tool": tool, "arguments": arguments}})


def _answer(
    *,
    role: str,
    status: str,
    decision: str,
    rule_id: str,
    paragraph_id: str | None,
    claim_ids: tuple[str, ...],
) -> str:
    """One role's final answer, as the raw JSON the parser reads.

    Written as a literal rather than through a pydantic model: the point of a
    regression batch is to exercise the *parsing* path, and a response built by
    serializing the schema it is about to be parsed into would pass even if the
    schema and the prompt had come apart.
    """
    provenance: dict[str, object] = {
        "upstream_claim_ids": list(claim_ids),
        "inherited_source_ids": [],
        "opened_paragraph_ids": [paragraph_id] if paragraph_id else [],
        "verification_basis": "self_checked" if paragraph_id else "upstream_only",
    }
    return json.dumps(
        {
            "target_clause_status": {category: status for category in _CATEGORIES},
            "decision": decision,
            "rule_id": rule_id,
            "evidence_provenance": provenance,
            "confidence": 0.9,
            "reason_summary": "Declared by the regression script; no inference is made.",
            "uncertainties": [],
            "role": role,
        }
    )


_CATEGORIES: tuple[str, ...] = ()


def _script_for(
    *,
    case,
    record,
    error_condition: ErrorCondition,
    source_access: bool,
    verification_required: bool,
    paragraph_id: str | None,
    adopted: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """The declared responses for one job's two roles."""
    status = case.gold_target_clause_status[case.target_category].value
    decision = case.gold_action.value

    def turns(role: str) -> tuple[str, ...]:
        answer = _answer(
            role=role,
            status=status,
            decision=decision,
            rule_id=case.policy.policy_id,
            paragraph_id=paragraph_id if source_access else None,
            claim_ids=adopted,
        )
        if not source_access:
            return (answer,)
        assert paragraph_id is not None
        categories = _CATEGORIES if verification_required else (case.target_category,)
        return (
            *(_tool_request(SEARCH_CONTRACT, query=category.replace("_", " "))
              for category in categories),
            _tool_request(OPEN_SOURCE_SPAN, paragraph_id=paragraph_id),
            answer,
        )

    return {"manager": turns("manager"), "compliance": turns("compliance")}


def build_regression(version: str, out_root: Path):
    """Build the set, the plan and the response script. No model call is made."""
    global _CATEGORIES  # noqa: PLW0603 - a module constant fixed once per build
    build = build_gate4_development_set(policy=POLICY_LOADERS[version]())
    _CATEGORIES = tuple(build.registry.get(build.registry.case_ids[0]).policy.target_clause_categories)

    records = {record.case_id: record for record in build.case_set.cases}
    positives = [r for r in build.case_set.cases if not r.is_negative_sentinel]
    sentinels = [r for r in build.case_set.cases if r.is_negative_sentinel]
    chosen = [
        *[r.case_id for r in positives[: SMALL_SLICE[0]]],
        *[r.case_id for r in sentinels[: SMALL_SLICE[1]]],
    ]

    plan = build_run_plan(
        experiment_id=EXPERIMENT_ID,
        registry=build.registry,
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
        case_ids=tuple(chosen),
    )

    script: dict[str, dict[str, tuple[str, ...]]] = {}
    for job in plan.jobs:
        case = build.registry.get(job.case_id)
        record = records[job.case_id]
        # The paragraph the case's own gold evidence falls in, so the node opens
        # something that really does state the clause it is answering about. A
        # sentinel has no gold evidence -- that is what makes it a sentinel --
        # so it opens the contract's first paragraph instead: a node with source
        # access that is about to report "nothing found" still has to have
        # looked, and opening the first paragraph is the cheapest honest way to
        # say it did.
        if record.paragraph_ids:
            paragraph_id = record.paragraph_ids[0]
        else:
            paragraphs = build.registry.document(job.case_id).paragraphs
            if not paragraphs:
                raise SystemExit(
                    f"{job.case_id} has no paragraphs, so its A1 cells cannot be "
                    "scripted; the batch would be a different experiment"
                )
            paragraph_id = paragraphs[0].paragraph_id
        adopted = (
            (record.omission_target_claim_id,)
            if job.error_condition is ErrorCondition.E0
            and record.omission_target_claim_id
            else ()
        )
        script[job.run_id] = _script_for(
            case=case,
            record=record,
            error_condition=job.error_condition,
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=paragraph_id,
            adopted=adopted,
        )

    return build, plan, script


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "outputs"))
    parser.add_argument(
        "--policy-version",
        choices=sorted(POLICY_LOADERS),
        default=DEFAULT_POLICY_VERSION,
        help="POLICY-01 revision to regress. Each writes its own directory.",
    )
    args = parser.parse_args(argv)

    version = args.policy_version
    out_root = Path(args.out)
    target = out_root / "development" / f"regression_v{version}"
    target.mkdir(parents=True, exist_ok=True)

    print(f"Gate 4.5 regression batch -- offline, no model call is made "
          f"(POLICY-01 revision {version})")
    print()

    build, plan, script = build_regression(version, out_root)
    paths = ExperimentPaths(root=target, experiment_id=EXPERIMENT_ID)

    # The runner refuses to overwrite a completed observation, and this script
    # does not work around that: a regression batch that silently replaced its
    # own previous run would make "the pipeline passed" unfalsifiable. Re-running
    # is a deliberate act, so it is left to the operator to clear the tree.
    existing = sorted(paths.raw_dir.glob("*/run.json"))
    recorded = [
        path for path in (
            target / "responses.json",
            target / "run_scores.jsonl",
            target / "summary.json",
            target / "regression_report.json",
        ) if path.exists()
    ]
    if existing or recorded:
        print(
            f"refusing to run: {target} already holds {len(existing)} completed "
            f"run(s) and {len(recorded)} result file(s).\n"
            "A regression batch does not overwrite its own observations. "
            "Point --out somewhere else to run it again.",
            file=sys.stderr,
        )
        return 1

    plan_path = plan.write_json(
        target / "manifests" / EXPERIMENT_ID / "run_plan.json"
    )
    script_path = target / "responses.json"
    script_path.write_text(
        json.dumps({run_id: {r: list(v) for r, v in roles.items()}
                    for run_id, roles in script.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  cases:  {', '.join(plan.case_ids)}")
    print(f"  plan:   {plan_path} ({len(plan.jobs)} job(s))")
    print(f"  script: {script_path}")
    print()

    runner = ExperimentRunner(
        plan=plan,
        registry=build.registry,
        conditions=load_conditions_v1(),
        workflow=load_workflow_v1(),
        models=load_models_v1(),
        client_factory=ScriptedClientFactory(script),
        paths=paths,
    )
    result = runner.run()
    print(f"  ran:    {len(result.completed)} completed, {len(result.failed)} failed")
    for job in result.failed:
        print(f"    FAILED {job.run_id}: {job.failure}")

    scores = score_experiment(paths.raw_dir, registry=build.registry)
    scores_path = scores.write_jsonl(target / "run_scores.jsonl")

    summary = summarise(scores, plan=plan, registry=build.registry)
    summary_path = target / "summary.json"
    summary_path.write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )

    report = {
        "policy_version": version,
        "experiment_id": EXPERIMENT_ID,
        "plan_fingerprint": plan.fingerprint(),
        "jobs": len(plan.jobs),
        "completed": len(result.completed),
        "failed": len(result.failed),
        "rows": len(scores.rows),
        "accuracy": summary.accuracy.model_dump(mode="json"),
        "error_survival_manager": summary.error_survival_manager.model_dump(mode="json"),
        "error_survival_compliance": summary.error_survival_compliance.model_dump(mode="json"),
        "by_condition": [
            condition.model_dump(mode="json") for condition in summary.by_condition
        ],
        "qc": summary.qc.model_dump(mode="json"),
    }
    report_path = target / "regression_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print(f"  scores:  {scores_path} ({len(scores.rows)} row(s))")
    print(f"  summary: {summary_path}")
    print(f"  report:  {report_path}")
    print()
    print("  A scripted batch proves the pipeline carries what it was handed. It")
    print("  is not a result and must not be reported as one.")

    ok = not result.failed and len(scores.rows) == len(plan.jobs)
    print()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

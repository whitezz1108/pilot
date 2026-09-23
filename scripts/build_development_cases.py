"""Build the Gate-4 development set: cases, review pack, and the run plan.

Offline. This script makes no model call and opens no socket. It reads the
frozen CUAD file, writes the case registry, the review pack and the run plan,
and stops -- running the plan is a separate, explicitly opted-in act.

    .venv/Scripts/python.exe scripts/build_development_cases.py

What it writes, under ``outputs/development/``:

``cases_v1.json``
    The Gate-3 case registry. This is what the plan, the runner and the scorer
    read.
``development_cases_v1.json``
    The experimenter-side case record: CUAD titles, selection reasons,
    exclusions, claim provenance, memo-pair diffs, review state.
``case_review_pack.md`` / ``case_review_pack.json``
    The human review pack. Every case is PENDING_HUMAN_REVIEW and nothing here
    can change that.
``selection_audit.json``
    The full eligibility audit: every contract considered, every reason it was
    kept out.
``memo_qc.json``
    §9's memo-pair matching report.

and, under ``outputs/manifests/<experiment_id>/``:

``run_plan.json`` / ``run_plan.csv``
    §11's plan, written **before** any live execution, with its fingerprint.

The script asserts the plan's size against the design's own arithmetic and
refuses to write a plan that does not match, because a plan of the wrong size is
the kind of thing that is discovered after it has been paid for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import (  # noqa: E402
    load_conditions_v1,
    load_models_v1,
    load_policy_v1,
    load_policy_v2,
    policy_fingerprint,
)
from pilot01.experiment.cli import (  # noqa: E402
    FULL_RUN_OPT_IN_ENV,
    FULL_RUN_OPT_IN_FLAG,
    LIVE_BATCH_JOB_THRESHOLD,
)
from pilot01.experiment.development import (  # noqa: E402
    build_diagnostics,  # noqa: F401  (re-exported for the operator's convenience)
    build_gate4_development_set,
    memo_qc_for,
    render_review_pack,
    write_review_pack,
)
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.runspec import (  # noqa: E402
    build_run_plan,
    write_run_plan_csv,
)

DEFAULT_OUTPUT_ROOT = "outputs"
DEVELOPMENT_DIR = "development"
DEFAULT_EXPERIMENT_ID = "gate4-development"
DEFAULT_PLAN_SEED = 0

POLICY_LOADERS = {"1": load_policy_v1, "2": load_policy_v2}
"""The policy revisions this script can build a set under, by version string.

Both are reachable on purpose. The default is revision 2, because a set built
for a new run must carry the rule that run is scored against. Revision 1 stays
reachable so the Gate-4 artifacts can be reproduced byte-for-byte from the same
script, which is what makes them auditable rather than merely stored.
"""

DEFAULT_POLICY_VERSION = "2"
"""The revision a new development set is built under unless one is named."""


def registry_filename(version: str = DEFAULT_POLICY_VERSION) -> str:
    """The registry filename one policy revision writes.

    The one place the naming rule lives. Revision 1 keeps the unsuffixed name it
    was written under, so the Gate-4 record's references to ``cases_v1.json``
    still resolve; every later revision writes its own file beside it. Anything
    that needs to *read* a registry -- the smoke, the checks -- asks here rather
    than spelling the name out, so a reader cannot pick up a file written by a
    different revision than the one the pipeline runs.
    """
    return f"cases_v1{'' if version == '1' else f'_v{version}'}.json"
"""The plan's *order* seed, which is a different decision from the selection seed.

Selection picks which contracts; the plan seed shuffles the order they run in.
Keeping them separate means a reader can tell which question a given seed
answers, and means changing the run order cannot silently change the cases.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--plan-seed", type=int, default=DEFAULT_PLAN_SEED)
    parser.add_argument(
        "--policy-version",
        choices=sorted(POLICY_LOADERS),
        default=DEFAULT_POLICY_VERSION,
        help=(
            "POLICY-01 revision to build the set under. Every revision writes "
            "its own filenames, so building revision 1 again reproduces the "
            "Gate-4 artifacts without overwriting the revision-2 ones."
        ),
    )
    args = parser.parse_args(argv)

    out_root = Path(args.out)
    development_dir = out_root / DEVELOPMENT_DIR
    development_dir.mkdir(parents=True, exist_ok=True)

    # Revision-suffixed artifact names. Revision 1 keeps the unsuffixed names it
    # was written under, so its files are still exactly where the Gate-4 record
    # says they are; every later revision gets its own set beside them.
    version = args.policy_version
    stem = "" if version == "1" else f"_v{version}"

    print(
        f"Gate 4 development build -- offline, no model call is made "
        f"(POLICY-01 revision {version})"
    )
    print()

    # -- 1-5. the offline pipeline, from the frozen annotations to the set --
    # One call into the development package, so the artifacts this script writes
    # and the cases the prompt review and the tests read are built by the same
    # code. A copy of the sequence here would be a second answer to "what is the
    # Gate-4 development set".
    build = build_gate4_development_set(policy=POLICY_LOADERS[version]())
    annotations = build.annotations
    policy_config = build.policy
    policy = policy_config.to_policy()
    documents = build.documents
    clause_spans = build.clause_spans
    selection = build.selection
    plans = build.plans
    case_set = build.case_set

    print(
        f"annotations: {len(annotations)} contract(s), "
        f"{len(annotations.all_categories())} clause categories, "
        f"digest {annotations.source_digest[:16]}"
    )
    print(
        f"policy: {policy.policy_id} v{policy.policy_version} "
        f"({policy_config.policy_status}), targets "
        f"{list(policy.target_clause_categories)}, "
        f"fingerprint {policy_fingerprint(policy_config)[:24]}"
    )
    print(f"source layer: {len(documents)} document(s) paragraphized")
    print(f"near-duplicate groups: {len(build.duplicate_of)} contract(s) marked as copies")
    print()
    print(f"selection: seed {selection.seed}, fingerprint {selection.fingerprint}")
    for key, count in sorted(selection.composition().items()):
        print(f"  {key}: {count}")
    print(f"  pool sizes: {dict((k, len(v)) for k, v in selection.candidates.items())}")
    print(f"  exclusions: {selection.exclusion_counts()}")

    (development_dir / f"selection_audit{stem}.json").write_text(
        json.dumps(selection.to_payload(), indent=2) + "\n", encoding="utf-8"
    )

    print(f"memos: {len(plans)} E0 memo(s) built")

    registry_path = case_set.registry.write_json(
        development_dir / registry_filename(version)
    )
    record_path = case_set.write_json(
        development_dir / f"development_cases_v1{stem}.json"
    )
    print(f"registry: {registry_path} ({len(case_set.registry)} case(s))")
    print(f"record:   {record_path}")

    # -- 6. memo QC --------------------------------------------------------
    qc, diffs = memo_qc_for(case_set)
    (development_dir / f"memo_qc{stem}.json").write_text(
        json.dumps(qc.to_payload(), indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"memo QC: {len(diffs)} pair(s), clean={qc.clean}, "
        f"max |Δ words|={qc.max_abs_word_delta}, max |Δ chars|={qc.max_abs_char_delta}"
    )
    for flag in qc.flags:
        print(f"  FLAG {flag}")

    # -- 7. review pack ----------------------------------------------------
    # The review pack is a directory rather than a filename: it writes a fixed
    # ``case_review_pack.{md,json}`` pair, so a revision that is not the first
    # gets its own subdirectory instead of a suffix.
    pack_dir = development_dir if version == "1" else development_dir / f"v{version}"
    markdown_path, json_path = write_review_pack(case_set, pack_dir)
    print(f"review pack: {markdown_path} ({len(render_review_pack(case_set))} chars)")
    print(f"             {json_path}")
    print("  every case is PENDING_HUMAN_REVIEW; no human review has been performed")

    # -- 8. the run plan, written before anything runs ---------------------
    conditions = load_conditions_v1()
    models = load_models_v1()
    plan = build_run_plan(
        experiment_id=args.experiment_id,
        registry=case_set.registry,
        conditions=conditions,
        repeat_count=1,
        seed=args.plan_seed,
        models=models,
    )

    expected = case_set.expected_run_count(
        condition_count=len(conditions.main_conditions), repeat_count=1
    )
    print()
    print(f"plan: {len(plan.jobs)} job(s) over {len(plan.case_ids)} case(s)")
    print(f"  conditions: {list(conditions.main_conditions)}")
    print(f"  repeats: {plan.repeat_count}, seed {plan.seed}")
    print(f"  model fingerprint: {plan.model_fingerprint}")

    if len(plan.jobs) != expected:
        print(
            f"REFUSING to write a plan of {len(plan.jobs)} job(s): the design's own "
            f"arithmetic gives {expected} (arms x conditions x repeats). A plan of "
            "the wrong size is discovered after it has been paid for.",
            file=sys.stderr,
        )
        return 1
    if expected != 60:
        print(
            f"note: the design gives {expected} run(s), not the 60 §11 declares. "
            "The plan was still written, because the arithmetic is derived from "
            "the cases rather than asserted; this line exists so the difference "
            "is visible rather than silent.",
            file=sys.stderr,
        )

    paths = ExperimentPaths(root=out_root, experiment_id=args.experiment_id)
    paths.manifest_dir.mkdir(parents=True, exist_ok=True)
    plan.write_json(paths.run_plan_json)
    write_run_plan_csv(plan, paths.run_plan_csv)
    print(f"  plan:     {paths.run_plan_json}")
    print(f"  plan csv: {paths.run_plan_csv}")
    print(f"  fingerprint: {plan.fingerprint()}")
    print()
    print("nothing was executed. Run the plan with:")
    print(
        f"  python -m pilot01.experiment run --registry {registry_path} "
        f"--plan {paths.run_plan_json} --out {out_root} --live "
        f"{FULL_RUN_OPT_IN_FLAG}"
    )
    print(
        f"  ({FULL_RUN_OPT_IN_FLAG} or {FULL_RUN_OPT_IN_ENV}=1 is required because "
        f"the plan holds {len(plan.jobs)} jobs, more than the "
        f"{LIVE_BATCH_JOB_THRESHOLD}-job smoke size)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Gate 4 §12: the real-case smoke, and its plan.

Between "one request reached the provider" and "spend sixty runs" there is a
gap, and this script is what stands in it. It takes one real development case
and drives it through the complete grid of cells the full batch will use:

    E0 x A0V0    the correct memo, no source access, no verification
    E1 x A0V0    the omission, no source access -- the error at its freest
    E1 x A1V0    the omission, with source access
    E1 x A1V1    the omission, with source access and verification enforced
    E0 x A1V0    the correct memo, with source access
    E0 x A1V1    the correct memo, with source access and verification enforced

Six runs, six calls to the same adapter the batch uses, and every one of them
scored by the same scorer. If a cell cannot reach the provider, cannot parse a
structured response, cannot isolate A0 from the source, or cannot reconstruct a
score, the smoke says so here -- for the price of six runs rather than sixty.

§12 names the first four cells. The last two are here because a plan covering
only those four is *incomplete* for its case, and the runner refuses an
incomplete plan rather than let an experiment silently skip cells. Exempting the
smoke from that check would weaken an invariant the batch depends on too, so the
smoke pays two extra runs to keep it -- and picks up coverage §12 did not ask
for, since a Manager holding a correct memo under A1V1 still has to decide
whether to search.

It is not a smaller version of the experiment and it produces no result: six
runs, one case, no repeat, nothing to compare against. What it produces is
permission to continue, or a reason not to.

    PILOT01_BASE_URL=... PILOT01_API_KEY=... \\
    .venv/Scripts/python.exe scripts/gate4_smoke.py --live

Nothing is sent without ``--live``: the default is to write the smoke plan, print
what it would cost in runs, and stop. The plan is six jobs, which is exactly the
smoke threshold, so it needs no batch opt-in -- see
:data:`pilot01.experiment.cli.LIVE_BATCH_JOB_THRESHOLD`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_conditions_v1, load_models_v1  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.runspec import RunPlan, RunSpec, build_run_plan  # noqa: E402
from pilot01.schemas import ErrorCondition  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_development_cases import registry_filename  # noqa: E402

DEFAULT_REGISTRY = REPO_ROOT / "outputs" / "development" / registry_filename()
"""The registry the smoke reads, at the revision the pipeline currently runs.

Asked of the build script rather than spelled out, so the smoke cannot be
pointed at a file written by a revision the prompts no longer match. Passing
``--registry`` explicitly still overrides it, which is how a re-run of the
Gate-4 smoke is reproduced.
"""

DEFAULT_CASE_ID = "DEV-POS-COC-0496"
"""The case the smoke uses unless another is named.

The first positive case in the registry, and a Change Of Control one, so the
smoke exercises a memo whose target claim was removed rather than a case where
nothing was at stake. It is pinned here rather than chosen per run so that two
operators smoke the same thing.
"""

SMOKE_CELLS: tuple[tuple[ErrorCondition, str], ...] = (
    (ErrorCondition.E0, "A0V0"),
    (ErrorCondition.E1, "A0V0"),
    (ErrorCondition.E1, "A1V0"),
    (ErrorCondition.E1, "A1V1"),
    (ErrorCondition.E0, "A1V0"),
    (ErrorCondition.E0, "A1V1"),
)
"""The cells this smoke runs: the complete grid for one case.

§12 names the first four -- one E0 to prove the correct-memo path runs, then E1
under each governance condition. The last two are here because a plan that
covered only four would be *incomplete*, and :meth:`RunPlan.check` refuses an
incomplete plan rather than letting an experiment silently skip cells.

That check is worth more than the two runs it costs. The alternative would be an
escape hatch that let a plan run without covering its own grid, and §12's own
rule is that a protocol problem must not be worked around by weakening the
design -- a completeness bypass is exactly that kind of weakening, and it would
be available to the batch as well as to the smoke.

So the smoke runs six instead of four, and gets two things §12 did not ask for:
E0 under A1V0 and A1V1, which exercise the tool loop with a *correct* memo
rather than an omitted one. That is a distinct path -- a Manager that has
nothing to escalate still has to decide whether to search -- and it is cheaper
to find out here than in the batch.
"""

SMOKE_EXPERIMENT_ID = "gate4-smoke"


def build_smoke_plan(
    *, registry: CaseRegistry, case_id: str = DEFAULT_CASE_ID
) -> RunPlan:
    """The six-cell plan, built from the full planner and then narrowed.

    Narrowed rather than built from scratch so that a smoke job is byte-identical
    to the batch job it stands in for: same run id, same model fingerprint, same
    flags. A smoke that ran a *different* job would be evidence about a run
    nobody is going to make.

    The plan is rebuilt for the smoke's own experiment id, so its raw artifacts
    land under their own directory and cannot be mistaken for batch results.
    """
    full = build_run_plan(
        experiment_id=SMOKE_EXPERIMENT_ID,
        registry=registry,
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
        case_ids=[case_id],
    )
    wanted = {(error_condition, condition_id) for error_condition, condition_id in SMOKE_CELLS}
    jobs: list[RunSpec] = [
        job
        for job in full.jobs
        if (job.error_condition, job.condition_id) in wanted
    ]
    if len(jobs) != len(SMOKE_CELLS):
        found = {(job.error_condition, job.condition_id) for job in jobs}
        missing = wanted - found
        raise SystemExit(
            f"error: case {case_id!r} cannot supply every smoke cell; missing "
            f"{sorted((e.value, c) for e, c in missing)}. A case that does not "
            "run both error conditions cannot be smoked with this script."
        )
    # Kept in the spec's order rather than the planner's randomized order: six
    # runs are read by a human, and the order is the diagnostic ladder.
    ordered = sorted(
        jobs,
        key=lambda job: SMOKE_CELLS.index((job.error_condition, job.condition_id)),
    )
    return RunPlan(
        experiment_id=full.experiment_id,
        seed=full.seed,
        repeat_count=full.repeat_count,
        condition_ids=tuple(dict.fromkeys(job.condition_id for job in ordered)),
        case_ids=(case_id,),
        models_version=full.models_version,
        model_fingerprint=full.model_fingerprint,
        jobs=tuple(ordered),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help=(
            "the development case registry. Defaults to the current policy "
            "revision's; pass outputs/development/cases_v1.json to reproduce "
            "the Gate-4 smoke."
        ),
    )
    parser.add_argument("--case", default=DEFAULT_CASE_ID, help="the case to smoke")
    parser.add_argument(
        "--plan-out",
        default=str(REPO_ROOT / "outputs" / "manifests" / SMOKE_EXPERIMENT_ID / "run_plan.json"),
        help="where to write the smoke plan",
    )
    parser.add_argument(
        "--out", default=str(REPO_ROOT / "outputs"), help="output root for raw artifacts"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually call the provider. Without it the plan is written and nothing is sent.",
    )
    parser.add_argument("--resume", action="store_true", help="resume an interrupted smoke")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        default=True,
        help=(
            "stop at the first failing cell (default). A smoke exists to find "
            "the first thing that is broken, and the first failure usually "
            "explains the rest -- paying for six identical failures to learn one "
            "fact is the opposite of what a smoke is for."
        ),
    )
    parser.add_argument(
        "--no-fail-fast",
        dest="fail_fast",
        action="store_false",
        help="run every cell even after one fails, to see the whole shape at once",
    )
    args = parser.parse_args(argv)

    registry = CaseRegistry.load_json(args.registry)
    plan = build_smoke_plan(registry=registry, case_id=args.case)

    plan_path = Path(args.plan_out)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan.write_json(plan_path)

    print(f"Gate 4 smoke plan -- {len(plan.jobs)} run(s), case {args.case}")
    for job in plan.jobs:
        print(
            f"  {job.error_condition.value} x {job.condition_id}  "
            f"(source_access={job.source_access}, "
            f"verification_required={job.verification_required})"
        )
    print(f"  plan: {plan_path}")
    print(f"  fingerprint: {plan.fingerprint()}")
    print()

    if not args.live:
        print("nothing was sent. Add --live to run these six jobs against the provider.")
        return 0

    # Handed to the same CLI an operator would use, rather than re-implementing
    # the run: the smoke must exercise the real path, including the credential
    # check and the batch gate.
    from pilot01.experiment.cli import main as cli_main

    return cli_main(
        [
            "run",
            "--registry",
            args.registry,
            "--plan",
            str(plan_path),
            "--out",
            args.out,
            "--live",
            *(["--resume"] if args.resume else []),
            *(["--fail-fast"] if args.fail_fast else []),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

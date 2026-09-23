"""The experiment command line.

Four commands, and one rule that shapes all of them: **the default does not
spend money.**

``plan``
    Enumerate the jobs and write the manifest. No model, no network.
``run``
    Execute the plan. Requires an explicit transport choice -- ``--responses``
    for a deterministic scripted run, or ``--live`` for real providers.
``score``
    Derive one row per run from the raw artifacts. No model, no network.
``summary``
    Count the scored rows. No model, no network.

There is no default transport, no fallback provider, and no environment
variable that silently turns a dry run into a live one. ``--live`` additionally
requires the endpoint and the credential the adapter reads from the
environment, and fails with the *name* of whichever is missing -- never its
value, which is never read into a message, printed, or written anywhere. This
module does not know those names: it asks
:func:`~pilot01.model.openai_compat.missing_credentials`, so the adapter stays
the only place in the repository that does.

Every command is a function of its arguments. Tests drive :func:`main` directly,
so nothing here needs a subprocess, a network, or a shipped registry file.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..config import (
    ConditionsConfig,
    ModelsConfig,
    WorkflowConfig,
    load_conditions_v1,
    load_models_v1,
    load_workflow_v1,
)
from ..model.client import ModelClientError
from ..model.openai_compat import OpenAICompatibleClient, missing_credentials
from .batch import (
    BatchError,
    ExperimentRunner,
    JobResult,
    JobStatus,
    ScriptedClientFactory,
    load_response_script,
    plan_or_raise,
)
from .cases import CaseRegistry, CaseRegistryError
from .layout import ExperimentPaths
from .runspec import RunPlan, RunPlanError, build_run_plan, write_run_plan_csv
from .scoring import PricingTable, ScoringError, score_experiment
from .summary import ExperimentSummary, summarise

__all__ = [
    "main",
    "build_parser",
    "LiveClientFactory",
    "LIVE_BATCH_JOB_THRESHOLD",
    "FULL_RUN_OPT_IN_ENV",
    "FULL_RUN_OPT_IN_FLAG",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

DEFAULT_OUTPUT_ROOT = "outputs"

LIVE_BATCH_JOB_THRESHOLD = 6
"""The largest live plan that counts as a smoke test.

Gate 4 §12 runs a real job or two by hand to prove the protocol works end to end
before anything larger is attempted. Any live plan bigger than that is a
*batch*, and a batch spends real money over real time -- so it needs the second,
separate opt-in below rather than following from ``--live`` alone.

Six is the threshold because it is exactly the size of the §12 smoke. §12 names
four cells -- one positive case through E0×A0V0, E1×A0V0, E1×A1V0 and E1×A1V1 --
but a plan covering only those four is *incomplete* for its case, and
:meth:`~pilot01.experiment.runspec.RunPlan.check` refuses an incomplete plan
rather than let an experiment silently skip cells. Bypassing that check for the
smoke would weaken an invariant the batch also depends on, so the smoke covers
the full six-cell grid for its one case instead and the threshold follows it.

It is a size limit rather than a case count, so it stays correct if the smoke's
shape changes again.
"""

FULL_RUN_OPT_IN_ENV = "PILOT01_GATE4_LIVE"
"""The environment opt-in for a paid batch beyond the smoke size.

Set to ``1`` to permit it. It is read as an opt-in only: it is never a way to
supply a credential, never read from a config file, and it never *reduces* what
is required. ``--live``, the credential, and this opt-in are three separate
conditions and all three must hold.

The equivalent CLI flag is ``run --live-batch``. Either satisfies the
requirement; neither substitutes for ``--live`` or for the credential.
"""

FULL_RUN_OPT_IN_FLAG = "--live-batch"


def _batch_opt_in_present(args: argparse.Namespace) -> bool:
    """Whether the operator opted in to a paid batch beyond the smoke size.

    Two ways to say yes, both explicit and both in this session's own command:
    the flag, or the environment variable. Neither is inferred from the presence
    of credentials, from a config file, or from a previous run's having worked.
    """
    import os

    if getattr(args, "live_batch", False):
        return True
    return os.environ.get(FULL_RUN_OPT_IN_ENV) == "1"


class LiveClientFactory:
    """Builds a real provider client per run, per role.

    The only place in the repository that constructs
    :class:`~pilot01.model.openai_compat.OpenAICompatibleClient`, and it is
    reached only from ``run --live``. The credential is read from the
    environment inside the adapter and never passes through this class, so
    there is nothing here that could print it, log it, or put it in a message.
    """

    def __init__(self, *, provider: str = "openai-compatible") -> None:
        self._provider = provider

    def __call__(self, *, role: str, job) -> OpenAICompatibleClient:  # noqa: ANN001
        return OpenAICompatibleClient(provider=self._provider)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pilot01.experiment",
        description=(
            "Run and score the omission-propagation pilot. Scoring is rule-based "
            "and offline; only 'run --live' reaches a provider."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, registry_required: bool = True) -> None:
        sub.add_argument(
            "--registry",
            required=registry_required,
            help="path to the case registry JSON written by the experiment build step",
        )
        sub.add_argument(
            "--out",
            default=DEFAULT_OUTPUT_ROOT,
            help=f"output root (default: {DEFAULT_OUTPUT_ROOT})",
        )

    plan = subparsers.add_parser("plan", help="enumerate the run plan")
    add_common(plan)
    plan.add_argument("--experiment-id", required=True)
    plan.add_argument("--repeat", type=int, default=1, help="repeats per cell (default: 1)")
    plan.add_argument("--seed", type=int, default=0, help="randomization seed (default: 0)")
    plan.add_argument(
        "--conditions",
        default=None,
        help="comma-separated condition ids (default: every main condition)",
    )
    plan.add_argument(
        "--cases", default=None, help="comma-separated case ids (default: every case)"
    )

    run = subparsers.add_parser("run", help="execute a plan")
    add_common(run)
    run.add_argument("--plan", required=True, help="path to run_plan.json")
    run.add_argument(
        "--responses",
        default=None,
        help="JSON file of scripted responses: {run_id: {role: [text, ...]}}",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help=(
            "call real providers, using the endpoint and credential named by the "
            "environment variables the adapter documents. Spends money."
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help="keep completed runs and clear interrupted ones out of the way",
    )
    run.add_argument(
        "--rerun-failed",
        action="store_true",
        help="with --resume, also re-run jobs whose completed run failed",
    )
    run.add_argument(
        "--live-batch",
        action="store_true",
        help=(
            "opt in to a paid live batch larger than the smoke size. Required, "
            f"together with --live and the credential, for any live plan over "
            f"{LIVE_BATCH_JOB_THRESHOLD} jobs. {FULL_RUN_OPT_IN_ENV}=1 does the same."
        ),
    )
    run.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    run.add_argument("--quiet", action="store_true", help="print only the final counts")

    score = subparsers.add_parser("score", help="derive one row per run from raw artifacts")
    add_common(score)
    score.add_argument("--experiment-id", required=True)
    score.add_argument(
        "--pricing", default=None, help="optional versioned pricing table (JSON)"
    )

    summary = subparsers.add_parser("summary", help="count the scored rows")
    add_common(summary)
    summary.add_argument("--experiment-id", required=True)
    summary.add_argument("--pricing", default=None)

    return parser


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_registry(path: str) -> CaseRegistry:
    return CaseRegistry.load_json(path)


def _pricing(path: str | None) -> PricingTable | None:
    return None if path is None else PricingTable.load_json(path)


def _split(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    return parts or None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_plan(args: argparse.Namespace) -> int:
    registry = _load_registry(args.registry)
    conditions: ConditionsConfig = load_conditions_v1()
    models: ModelsConfig = load_models_v1()

    plan = build_run_plan(
        experiment_id=args.experiment_id,
        registry=registry,
        conditions=conditions,
        repeat_count=args.repeat,
        seed=args.seed,
        models=models,
        condition_ids=_split(args.conditions),
        case_ids=_split(args.cases),
    )

    paths = ExperimentPaths(root=Path(args.out), experiment_id=args.experiment_id).ensure()
    plan.write_json(paths.run_plan_json)
    write_run_plan_csv(plan, paths.run_plan_csv)

    print(
        f"planned {len(plan.jobs)} run(s): {len(plan.case_ids)} case(s) x "
        f"{len(plan.condition_ids)} condition(s) x {plan.repeat_count} repeat(s), "
        f"seed {plan.seed}"
    )
    print(f"plan:    {paths.run_plan_json}")
    print(f"plan csv:{paths.run_plan_csv}")
    print(f"fingerprint: {plan.fingerprint()}")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    if args.live and args.responses:
        print(
            "error: --live and --responses are different transports; choose one",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not args.live and not args.responses:
        print(
            "error: 'run' needs a transport. Pass --responses <file> for a "
            "deterministic scripted run, or --live to call real providers. There "
            "is no default, because a default would be the one that spends money.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    registry = _load_registry(args.registry)
    conditions = load_conditions_v1()
    workflow: WorkflowConfig = load_workflow_v1()
    models = load_models_v1()
    plan = plan_or_raise(args.plan)

    if args.live:
        # Asked of the adapter rather than read here: the adapter is the only
        # module that knows a credential's name, and a second reader of the
        # environment would be a second place for it to leak from.
        missing = missing_credentials()
        if missing:
            # The variable's name, never its value. A missing credential must
            # fail before the first paid call, not at it.
            print(
                "error: --live needs "
                + " and ".join(missing)
                + " to be set in the environment. Neither is read from a config "
                "file, and neither is ever printed.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        # The third condition, and a separate one. Credentials existing is not a
        # decision to spend them: a live plan bigger than the smoke size is a
        # batch, and a batch runs only when the operator says so in this
        # session. Checked *after* the credential check so that the message a
        # missing credential produces is never masked by this one.
        if len(plan.jobs) > LIVE_BATCH_JOB_THRESHOLD and not _batch_opt_in_present(args):
            print(
                f"error: this live plan holds {len(plan.jobs)} jobs, more than the "
                f"{LIVE_BATCH_JOB_THRESHOLD}-job smoke size, so it needs a second "
                "explicit opt-in on top of --live and the credential. Pass "
                f"{FULL_RUN_OPT_IN_FLAG} or set {FULL_RUN_OPT_IN_ENV}=1. Nothing was "
                "sent.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        factory = LiveClientFactory()
        transport = "live"
    else:
        factory = ScriptedClientFactory(load_response_script(args.responses))
        transport = f"scripted ({args.responses})"

    paths = ExperimentPaths(root=Path(args.out), experiment_id=plan.experiment_id)

    def progress(position: int, total: int, result: JobResult) -> None:
        if args.quiet:
            return
        suffix = "" if result.detail is None else f" ({result.detail})"
        print(f"[{position}/{total}] {result.run_id}: {result.status.value}{suffix}")

    runner = ExperimentRunner(
        plan=plan,
        registry=registry,
        conditions=conditions,
        workflow=workflow,
        models=models,
        client_factory=factory,
        paths=paths,
        progress=progress,
    )
    batch = runner.run(
        resume=args.resume, rerun_failed=args.rerun_failed, fail_fast=args.fail_fast
    )
    batch.write_json(paths.batch_index_json)

    counts = ", ".join(f"{name}={count}" for name, count in sorted(batch.counts.items()))
    print(f"transport: {transport}")
    print(f"runs: {counts}")
    print(f"raw:  {paths.raw_dir}")
    return EXIT_OK


def _cmd_score(args: argparse.Namespace) -> int:
    registry = _load_registry(args.registry)
    paths = ExperimentPaths(root=Path(args.out), experiment_id=args.experiment_id)
    scores = score_experiment(
        paths.raw_dir, registry=registry, pricing=_pricing(args.pricing)
    )
    scores.write_jsonl(paths.run_scores_jsonl)
    scores.write_csv(paths.run_scores_csv)

    print(f"scored {len(scores)} run(s)")
    print(f"rows: {paths.run_scores_jsonl}")
    print(f"csv:  {paths.run_scores_csv}")
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    registry = _load_registry(args.registry)
    paths = ExperimentPaths(root=Path(args.out), experiment_id=args.experiment_id)
    scores = score_experiment(
        paths.raw_dir, registry=registry, pricing=_pricing(args.pricing)
    )
    # The plan is read only if it happens to be there, and only for the QC
    # block's cell-completeness checks. Its absence is not an error: a summary
    # of the runs that exist is still a summary, and §10's point is that the
    # scored numbers never depend on the plan.
    plan = RunPlan.load_json(paths.run_plan_json) if paths.run_plan_json.exists() else None
    summary = summarise(scores, plan=plan, registry=registry)
    summary.write_json(paths.summary_json)

    print(_format_summary(summary))
    print(f"summary: {paths.summary_json}")
    return EXIT_OK


def _format_summary(summary: ExperimentSummary) -> str:
    lines = [
        f"experiment {summary.experiment_id}: {summary.runs} run(s), "
        f"{summary.completed} completed, {summary.protocol_failures} protocol failure(s), "
        f"{summary.unanswered} unanswered",
        f"accuracy:                {summary.accuracy.describe()}",
        f"error survival (mgr):    {summary.error_survival_manager.describe()}",
        f"error survival (comp):   {summary.error_survival_compliance.describe()}",
        "correction stages:       "
        + ", ".join(f"{k}={v}" for k, v in summary.correction_stages.items()),
    ]
    for condition in summary.by_condition:
        lines.append(
            f"  {condition.condition_id}: n={condition.runs} "
            f"accuracy={condition.accuracy.describe()} "
            f"survival(mgr)={condition.error_survival_manager.describe()} "
            f"survival(comp)={condition.error_survival_compliance.describe()}"
        )
    return "\n".join(lines)


_COMMANDS = {
    "plan": _cmd_plan,
    "run": _cmd_run,
    "score": _cmd_score,
    "summary": _cmd_summary,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the process exit code.

    Every expected failure -- a bad registry, an inconsistent plan, a run
    directory that already exists, a missing pricing entry -- is reported as a
    message and a non-zero code rather than a traceback, because these are
    operator errors and a stack trace tells an operator nothing they can act on.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _COMMANDS[args.command](args)
    except (
        BatchError,
        CaseRegistryError,
        RunPlanError,
        ScoringError,
        ModelClientError,
        FileNotFoundError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

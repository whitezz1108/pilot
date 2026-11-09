"""Offline process metrics for a frozen recovery experiment."""
import argparse
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.recovery_analysis import write_analysis
from pilot01.experiment.runspec import RunPlan
from pilot01.runtime import ExperimentProfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=1109)
    args = parser.parse_args()
    if args.bootstrap_samples < 0:
        parser.error("bootstrap-samples must be nonnegative")
    plan = RunPlan.load_json(args.plan)
    registry = CaseRegistry.load_json(args.registry)
    profile = ExperimentProfile(args.profile)
    plan.check(profile.conditions, registry)
    summary = write_analysis(plan, registry, args.raw, args.output,
                             bootstrap_samples=args.bootstrap_samples, seed=args.seed)
    print(f"Analyzed {len(plan.jobs)} planned runs. Batch complete: {summary['batch_complete']}")
    print(args.output.resolve())


if __name__ == "__main__":
    main()

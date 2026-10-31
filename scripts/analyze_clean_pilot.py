"""Offline v2 metrics over the complete frozen plan, including missing runs."""
import argparse
from pathlib import Path

from pilot01.runtime import ExperimentProfile
from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.runspec import RunPlan
from pilot01.experiment.scoring_v2 import write_report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "registry", "raw", "out"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--profile", default="config/experiment_v2.yaml")
    p.add_argument("--pricing", default=None)
    args = p.parse_args()
    plan, registry = RunPlan.load_json(args.plan), CaseRegistry.load_json(args.registry)
    profile = ExperimentProfile(Path(args.profile))
    if plan.profile_fingerprint != profile.fingerprint():
        raise ValueError("frozen implementation/profile changed")
    plan.check(profile.conditions, registry)
    from pilot01.experiment.scoring import PricingTable
    summary = write_report(plan, registry, args.raw, args.out, PricingTable.load_json(args.pricing) if args.pricing else None)
    print(summary["overall"])


if __name__ == "__main__":
    main()

"""Narrow one generated run plan to one explicit error condition."""
from __future__ import annotations

import argparse
from pathlib import Path

from pilot01.experiment.runspec import ErrorCondition, RunPlan, write_run_plan_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--error", choices=("E0", "E1"), required=True)
    args = parser.parse_args()

    plan = RunPlan.load_json(args.plan)
    error = ErrorCondition(args.error)
    jobs = tuple(job for job in plan.jobs if job.error_condition is error)
    if len(jobs) != 1:
        raise ValueError(f"expected exactly one {error.value} job, found {len(jobs)}")
    narrowed = plan.model_copy(
        update={"jobs": jobs, "selected_error_conditions": (error,)}
    )
    args.plan.write_text(narrowed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_run_plan_csv(narrowed, args.plan.with_suffix(".csv"))


if __name__ == "__main__":
    main()

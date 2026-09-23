"""Gate 4 §12: the ten checks the real-case smoke has to pass.

The smoke itself (`scripts/gate4_smoke.py`) spends the runs. This reads what
they produced and answers the spec's question: does the protocol work end to
end on a real contract, or does something have to be fixed before sixty runs are
bought?

§12 names ten things to check. Each is a check here, each reads the raw
artifacts rather than re-running anything, and each says what it found rather
than only whether it passed:

  1. requests reached only the intended model
  2. no secrets in the logs
  3. structured parsing succeeded
  4. no unexpected format-repair storm
  5. A0 could not access the source
  6. A1 could
  7. A1V1 actually searched and opened evidence
  8. scoring reconstructs from the raw tree
  9. the raw files are complete
 10. final run rows were generated

It runs offline against an existing smoke directory, so it can be re-run after
the fact, and it is what the Gate-4 tests exercise instead of a live call.

    .venv/Scripts/python.exe scripts/gate4_smoke_check.py

Exit status is 0 when every check passes and 1 otherwise, so it can gate the
batch. A failure here means STOP -- §12 is explicit that a protocol failure must
not be worked around by weakening the design.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_models_v1  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.scoring import score_experiment  # noqa: E402

SMOKE_EXPERIMENT_ID = "gate4-smoke"

REQUIRED_RUN_FILES = (
    "run.json",
    "events.jsonl",
    "model_calls.jsonl",
    "tool_calls.jsonl",
    "verification.jsonl",
)

# A log that is legitimately empty in some cells is not evidence of a truncated
# write. ``tool_calls.jsonl`` is the case that matters: a run offered no source
# makes no tool call, so an empty file there is the *isolation* working -- which
# is what check 5 asserts. Requiring it non-empty everywhere would have failed
# every A0 run for being correctly isolated.
FILES_THAT_MUST_BE_NON_EMPTY = (
    "events.jsonl",
    "model_calls.jsonl",
    "verification.jsonl",
)

# A "format-repair storm" is not one repair -- a single repair is the machinery
# working. It is repairs on most calls, which means the prompt and the schema
# have come apart and every call is being paid for twice.
MAX_REPAIR_FRACTION = 0.25

CREDENTIAL_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.]{12,}"),
    re.compile(r"PILOT01_API_KEY\s*[:=]\s*\S"),
    re.compile(r'"(api_?key|authorization|secret|password)"\s*:\s*"[^"]+"', re.I),
)


class Check:
    """One named check, its outcome, and the evidence for it."""

    def __init__(self, number: int, name: str) -> None:
        self.number = number
        self.name = name
        self.details: list[str] = []
        self.failures: list[str] = []

    def note(self, text: str) -> None:
        self.details.append(text)

    def fail(self, text: str) -> None:
        self.failures.append(text)

    def require(self, condition: bool, ok: str, bad: str) -> bool:
        (self.note if condition else self.fail)(ok if condition else bad)
        return condition

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "name": self.name,
            "passed": self.passed,
            "details": self.details,
            "failures": self.failures,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "outputs"))
    parser.add_argument("--experiment-id", default=SMOKE_EXPERIMENT_ID)
    parser.add_argument(
        "--registry",
        default=str(REPO_ROOT / "outputs" / "development" / "cases_v1.json"),
    )
    parser.add_argument(
        "--report",
        default=str(REPO_ROOT / "outputs" / "development" / "smoke_check.json"),
    )
    args = parser.parse_args(argv)

    paths = ExperimentPaths(root=Path(args.out), experiment_id=args.experiment_id)
    registry = CaseRegistry.load_json(args.registry)
    models = load_models_v1()

    runs = _read_runs(paths)
    if not runs:
        print(f"error: no runs under {paths.raw_dir}; run the smoke first")
        return 1

    checks = [
        _check_intended_model(runs, models),
        _check_no_secrets(paths, runs, Path(args.registry)),
        _check_parsing(runs),
        _check_no_repair_storm(runs),
        _check_a0_isolated(runs),
        _check_a1_reached_source(runs),
        _check_a1v1_verified(runs),
        _check_scoring_reconstructs(paths, registry),
        _check_raw_files_complete(runs),
        _check_run_rows(paths, runs),
    ]

    report = {
        "experiment_id": args.experiment_id,
        "runs": len(runs),
        "checks": [check.as_dict() for check in checks],
        "findings": _findings(runs, paths),
        "passed": all(check.passed for check in checks),
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    _print_report(report, report_path)
    return 0 if report["passed"] else 1


# ---------------------------------------------------------------------------
# reading the raw tree
# ---------------------------------------------------------------------------


def _read_runs(paths: ExperimentPaths) -> list[dict]:
    runs = []
    if not paths.raw_dir.exists():
        return runs
    for directory in sorted(paths.raw_dir.iterdir()):
        run_json = directory / "run.json"
        if not directory.is_dir() or not run_json.exists():
            continue
        artifact = json.loads(run_json.read_text(encoding="utf-8"))
        calls = _jsonl(directory / "model_calls.jsonl")
        runs.append(
            {
                "dir": directory,
                "artifact": artifact,
                "calls": calls,
                "tool_calls": _jsonl(directory / "tool_calls.jsonl"),
                "verifications": _jsonl(directory / "verification.jsonl"),
                "events": _jsonl(directory / "events.jsonl"),
                "execution": artifact.get("execution") or {},
                "failure": artifact.get("failure") or {},
            }
        )
    return runs


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _cell(run: dict) -> str:
    return f"{run['artifact'].get('error_condition')} x {run['artifact'].get('condition_id')}"


# ---------------------------------------------------------------------------
# 1. requests reached only the intended model
# ---------------------------------------------------------------------------


def _check_intended_model(runs: list[dict], models) -> Check:
    check = Check(1, "requests reached only the intended model")
    intended = {
        role: config.model_id for role, config in models.roles.items()
    }
    for run in runs:
        for call in run["calls"]:
            role = call["role"]
            seen = call["params"]["model_id"]
            if seen != intended.get(role):
                check.fail(
                    f"{_cell(run)}: {role} call used model {seen!r}, "
                    f"but models_v1 declares {intended.get(role)!r}"
                )
    if check.passed:
        check.note(
            f"every call to every role used the model models_v1 declares: "
            f"{sorted(set(intended.values()))}"
        )
    check.note(f"{sum(len(run['calls']) for run in runs)} call(s) inspected")
    return check


# ---------------------------------------------------------------------------
# 2. no secrets in the logs
# ---------------------------------------------------------------------------


def _check_no_secrets(
    paths: ExperimentPaths, runs: list[dict], registry_path: Path
) -> Check:
    check = Check(2, "no secrets in the logs")
    import os

    from pilot01.model.openai_compat import DEFAULT_API_KEY_ENV

    key = os.environ.get(DEFAULT_API_KEY_ENV) or ""
    scanned = 0
    for run in runs:
        for path in sorted(run["dir"].iterdir()):
            if not path.is_file():
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            if key and key in text:
                check.fail(f"{path.name} of {_cell(run)} contains the credential itself")
            for pattern in CREDENTIAL_SHAPES:
                match = pattern.search(text)
                if match:
                    check.fail(
                        f"{path.name} of {_cell(run)} matches credential shape "
                        f"{pattern.pattern!r}"
                    )
    # The plan and the registry are shipped alongside the runs and must be clean
    # for the same reason.
    for path in (paths.run_plan_json, paths.run_plan_csv, registry_path):
        if path.exists():
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in CREDENTIAL_SHAPES:
                if pattern.search(text):
                    check.fail(f"{path.name} matches credential shape {pattern.pattern!r}")
    if check.passed:
        check.note(f"no credential value and no credential shape in {scanned} file(s)")
    return check


# ---------------------------------------------------------------------------
# 3. structured parsing succeeded
# ---------------------------------------------------------------------------


def _check_parsing(runs: list[dict]) -> Check:
    check = Check(3, "structured parsing succeeded")
    for run in runs:
        if run["failure"].get("model_output_failure"):
            check.fail(
                f"{_cell(run)} could not be parsed: {run['failure'].get('error')}"
            )
        if run["failure"].get("protocol_failure"):
            check.fail(
                f"{_cell(run)} failed the protocol at "
                f"{run['failure'].get('failure_stage')}: {run['failure'].get('error')}"
            )
    for run in runs:
        for call in run["calls"]:
            if call.get("is_tool_turn"):
                continue  # a tool turn parses into no domain object by design
            if call.get("parse_error"):
                check.fail(
                    f"{_cell(run)} {call['role']} call {call['attempt']} "
                    f"did not parse: {call['parse_error']}"
                )
    if check.passed:
        check.note(f"every one of {len(runs)} run(s) produced a valid domain object")
    return check


# ---------------------------------------------------------------------------
# 4. no unexpected format-repair storm
# ---------------------------------------------------------------------------


def _check_no_repair_storm(runs: list[dict]) -> Check:
    check = Check(4, "no unexpected format-repair storm")
    calls = [call for run in runs for call in run["calls"]]
    repairs = [call for call in calls if call.get("is_repair")]
    fraction = len(repairs) / len(calls) if calls else 0.0
    check.note(
        f"{len(repairs)} repair call(s) out of {len(calls)} call(s) "
        f"({fraction:.0%}); the limit is {MAX_REPAIR_FRACTION:.0%}"
    )
    if fraction > MAX_REPAIR_FRACTION:
        check.fail(
            f"{fraction:.0%} of calls needed a format repair, above the "
            f"{MAX_REPAIR_FRACTION:.0%} limit: the prompt and the schema have "
            "come apart and every call is being paid for twice"
        )
    for run in runs:
        for call in run["calls"]:
            if call.get("finish_reason") == "length":
                check.fail(
                    f"{_cell(run)} {call['role']} call was truncated at "
                    f"{call['params']['max_output_tokens']} tokens; a truncated "
                    "run is a budget fault and its answer is not an observation"
                )
    if check.passed:
        check.note("no call was truncated at the output limit")
    return check


# ---------------------------------------------------------------------------
# 5. A0 could not access the source
# ---------------------------------------------------------------------------


def _check_a0_isolated(runs: list[dict]) -> Check:
    check = Check(5, "A0 could not access the source")
    a0 = [run for run in runs if not run["artifact"].get("source_access")]
    check.require(bool(a0), f"{len(a0)} A0 run(s) to check", "no A0 run in the smoke")
    for run in a0:
        for call in run["tool_calls"]:
            if call.get("available") is not False:
                check.fail(
                    f"{_cell(run)} ran a tool call with available="
                    f"{call.get('available')!r}; A0 must be offered no source"
                )
        if run["tool_calls"]:
            check.fail(
                f"{_cell(run)} recorded {len(run['tool_calls'])} tool call(s) "
                "under A0"
            )
        opened = (run["execution"].get("manager_output") or {}).get("evidence_ids") or []
        if opened:
            check.note(
                f"{_cell(run)} cited {len(opened)} evidence id(s) from the memo "
                "without opening the source -- the memo's own citations, which is "
                "what A0 permits"
            )
    if check.passed:
        check.note("no A0 run reached a source tool")
    return check


# ---------------------------------------------------------------------------
# 6. A1 could
# ---------------------------------------------------------------------------


def _check_a1_reached_source(runs: list[dict]) -> Check:
    check = Check(6, "A1 could access the source")
    a1 = [run for run in runs if run["artifact"].get("source_access")]
    check.require(bool(a1), f"{len(a1)} A1 run(s) to check", "no A1 run in the smoke")
    for run in a1:
        if not run["tool_calls"]:
            check.fail(f"{_cell(run)} is A1 but made no tool call at all")
            continue
        unavailable = [c for c in run["tool_calls"] if c.get("available") is not True]
        if unavailable:
            check.fail(
                f"{_cell(run)} ran {len(unavailable)} tool call(s) with the source "
                "unavailable"
            )
    if check.passed:
        check.note(
            "every A1 run reached the source: "
            + ", ".join(f"{_cell(r)}={len(r['tool_calls'])}" for r in a1)
        )
    return check


# ---------------------------------------------------------------------------
# 7. A1V1 actually searched and opened evidence
# ---------------------------------------------------------------------------


def _check_a1v1_verified(runs: list[dict]) -> Check:
    check = Check(7, "A1V1 actually searched and opened evidence")
    v1 = [run for run in runs if run["artifact"].get("verification_required")]
    check.require(bool(v1), f"{len(v1)} A1V1 run(s) to check", "no A1V1 run in the smoke")
    for run in v1:
        tools_used = {call.get("tool") for call in run["tool_calls"]}
        if "search_contract" not in tools_used:
            check.fail(f"{_cell(run)} never searched the contract")
        if "open_source_span" not in tools_used:
            check.fail(f"{_cell(run)} never opened a passage")
        for record in run["verifications"]:
            if record.get("required") and record.get("status") != "verified":
                check.fail(
                    f"{_cell(run)} {record.get('node')} verification was required "
                    f"but ended {record.get('status')!r}: {record.get('note')}"
                )
        if not run["verifications"]:
            check.fail(f"{_cell(run)} recorded no verification at all")
    if check.passed:
        check.note(
            "every A1V1 run searched, opened a passage, and verified: "
            + ", ".join(f"{_cell(r)}={len(r['verifications'])}" for r in v1)
        )
    return check


# ---------------------------------------------------------------------------
# 8. scoring reconstructs from the raw tree
# ---------------------------------------------------------------------------


def _check_scoring_reconstructs(paths: ExperimentPaths, registry) -> Check:
    check = Check(8, "scoring reconstructs from the raw tree")
    try:
        scores = score_experiment(paths.raw_dir, registry=registry)
    except Exception as exc:  # noqa: BLE001 - the check reports, it does not raise
        check.fail(f"the scorer could not read the raw tree: {type(exc).__name__}: {exc}")
        return check
    check.note(f"{len(scores.rows)} score row(s) reconstructed from raw artifacts alone")
    if not scores.rows:
        check.fail("the scorer produced no rows")
    # Scoring twice must agree: a scorer that depends on anything but the raw
    # tree would not be reproducible, and §14 uses it unchanged.
    again = score_experiment(paths.raw_dir, registry=registry)
    if scores.model_dump_json() != again.model_dump_json():
        check.fail("two scoring passes over the same raw tree disagreed")
    else:
        check.note("two scoring passes over the same raw tree were identical")
    return check


# ---------------------------------------------------------------------------
# 9. the raw files are complete
# ---------------------------------------------------------------------------


def _check_raw_files_complete(runs: list[dict]) -> Check:
    check = Check(9, "the raw files are complete")
    for run in runs:
        for name in REQUIRED_RUN_FILES:
            if not (run["dir"] / name).exists():
                check.fail(f"{_cell(run)} is missing {name}")
        for name in FILES_THAT_MUST_BE_NON_EMPTY:
            path = run["dir"] / name
            if path.exists() and path.stat().st_size == 0:
                check.fail(f"{_cell(run)} has an empty {name}")
        # The tool log must be empty exactly when no source was offered, and
        # non-empty exactly when one was. Both directions matter: an A1 run with
        # no tool log never reached the source, and an A0 run with one reached
        # something it should not have.
        tool_log = run["dir"] / "tool_calls.jsonl"
        offered = bool(run["artifact"].get("source_access"))
        if tool_log.exists():
            has_records = tool_log.stat().st_size > 0
            if offered and not has_records:
                check.fail(f"{_cell(run)} is A1 but its tool log is empty")
            if not offered and has_records:
                check.fail(f"{_cell(run)} is A0 but its tool log is not empty")
        if not run["calls"]:
            check.fail(f"{_cell(run)} recorded no model call")
        for call in run["calls"]:
            if call.get("raw_response") is None and not call.get("error"):
                check.fail(
                    f"{_cell(run)} call {call.get('call_index')} recorded neither "
                    "a response nor an error"
                )
    if check.passed:
        check.note(
            f"all {len(REQUIRED_RUN_FILES)} artifact files present and non-empty "
            f"for all {len(runs)} run(s)"
        )
    return check


# ---------------------------------------------------------------------------
# 10. final run rows were generated
# ---------------------------------------------------------------------------


def _check_run_rows(paths: ExperimentPaths, runs: list[dict]) -> Check:
    check = Check(10, "final run rows were generated")
    if not paths.run_scores_jsonl.exists():
        check.fail(
            f"{paths.run_scores_jsonl} does not exist; score the smoke before "
            "checking its rows"
        )
        return check
    rows = _jsonl(paths.run_scores_jsonl)
    check.note(f"{len(rows)} row(s) in {paths.run_scores_jsonl.name}")
    if len(rows) != len(runs):
        check.fail(f"{len(rows)} score row(s) for {len(runs)} run(s)")
    scored = {row.get("run_id") for row in rows}
    for run in runs:
        if run["artifact"].get("run_id") not in scored:
            check.fail(f"{_cell(run)} has no score row")
    for row in rows:
        for field in ("final_action_correct", "false_escalation"):
            if field not in row:
                check.fail(f"score row {row.get('run_id')} has no {field!r}")
    if check.passed:
        check.note("every run has a score row carrying the §14 metric fields")
    return check


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _findings(runs: list[dict], paths: ExperimentPaths) -> list[dict]:
    """What the smoke taught, recorded rather than acted on.

    §16 separates three kinds of thing, and the smoke produced one of each. They
    are written down here because the alternative -- noticing something in a
    terminal and remembering it -- is how a development gate turns into a
    confirmatory claim nobody can check.

    Nothing in this function changes a prompt, a policy, a case or a condition.
    §16 forbids responding to an outcome difference by editing the experiment,
    and the design finding below is an outcome difference: it is stated, it is
    attributed to the experiment rather than to a defect, and nothing follows
    from it here.
    """
    findings: list[dict] = [
        {
            "id": "GATE4-IMPL-01",
            "kind": "implementation",
            "status": "fixed",
            "title": "the adapter dropped finish_reason, so truncation read as a schema failure",
            "detail": (
                "The provider is a reasoning model: it spends output tokens before "
                "emitting content, and max_output_tokens caps both. At the original "
                "budget of 1024 the E0 x A1V0 cell returned finish_reason='length' "
                "with an empty string at exactly 1024 completion tokens. Because the "
                "adapter discarded finish_reason, that arrived downstream as an empty "
                "response -- a format failure -- which triggers the one permitted "
                "format repair and spends a second paid call to truncate again. It "
                "would have been recorded as a model that cannot follow a schema."
            ),
            "evidence": (
                "Six-cell smoke at max_output_tokens=1024: five cells completed, one "
                "failed at the manager stage with finish_reason='length'. Completed "
                "calls used 465-846 completion tokens."
            ),
            "action_taken": (
                "ModelResponse.finish_reason and ModelCallRecord.finish_reason added; "
                "call_structured raises a truncation error naming the budget instead "
                "of attempting a repair; max_output_tokens 1024 -> 4096 and "
                "timeout 60s -> 180s in config/models_v1.yaml. Re-ran the smoke: all "
                "six cells completed, every call finish_reason='stop'."
            ),
            "changes_the_design": False,
        },
        {
            "id": "GATE4-DESIGN-01",
            "kind": "design",
            "status": "open",
            "title": "POLICY-01's UNKNOWN->ESCALATE rule absorbed the omission in the A0 arm",
            "detail": (
                "For the smoke's positive case the gold clause is present, so the "
                "gold action is ESCALATE. The E1 memo removes the claim that the "
                "clause is present, and under A0 the Manager has no way to look. It "
                "reported clause_status='unknown', which the policy maps to ESCALATE "
                "-- the gold action. The run scores correct while the omission "
                "survived both agents uncorrected (error_survival_manager=True, "
                "error_survival_compliance=True, correction_stage='never')."
            ),
            "evidence": (
                "gate4-smoke run DEV-POS-COC-0496 E1 x A0V0: final_action_correct=True, "
                "error_survival_manager=True, error_survival_compliance=True, "
                "correction_stage=never."
            ),
            "action_taken": (
                "NONE, deliberately. §16 forbids rewriting prompts or policy in "
                "response to an outcome difference, and one case at one repeat is not "
                "evidence of a pattern. Recorded for the §16 verdict and for the "
                "confirmatory gate's design review."
            ),
            "changes_the_design": False,
            "why_it_matters": (
                "final_action_correct cannot separate a decision that was right from "
                "one that was right by accident, on this case. error_survival_* and "
                "correction_stage can, and they carry the signal. Whether the pattern "
                "holds across the eight positive cases is what the 60-run batch is "
                "for -- which is a reason to run it, not a reason to change it first."
            ),
        },
        {
            "id": "GATE4-DESIGN-02",
            "kind": "design",
            "status": "observed",
            "title": "source access recovered the omission at the Manager stage",
            "detail": (
                "Under A1V0 and A1V1 the Manager searched the contract, opened the "
                "gold paragraph, and reported the clause present -- reversing the "
                "omission before Compliance ever saw it. correction_stage='manager' "
                "in both cells."
            ),
            "evidence": (
                "E1 x A1V0 and E1 x A1V1: error_survival_manager=False, "
                "correction_stage=manager, 4 tool calls each, verification satisfied."
            ),
            "action_taken": "NONE. This is the mechanism working as designed.",
            "changes_the_design": False,
        },
    ]

    # The two cells the smoke did not run are named, so the record cannot be read
    # as covering more than it does.
    cells = sorted(
        f"{run['artifact'].get('error_condition')} x {run['artifact'].get('condition_id')}"
        for run in runs
    )
    findings.append(
        {
            "id": "GATE4-SCOPE-01",
            "kind": "scope",
            "status": "informational",
            "title": "the smoke is one case, one repeat, and is not a result",
            "detail": (
                "Cells run: " + ", ".join(cells) + ". One positive case, repeat 1. "
                "It produces no comparison and supports no statement about agents; "
                "what it supports is the claim that the protocol runs end to end on "
                "a real contract."
            ),
            "evidence": f"{len(runs)} run(s) under {paths.raw_dir}",
            "action_taken": "NONE.",
            "changes_the_design": False,
        }
    )
    return findings


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _print_report(report: dict, path: Path) -> None:
    print(f"Gate 4 smoke check -- {report['runs']} run(s), experiment "
          f"{report['experiment_id']}")
    print()
    for check in report["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"[{mark}] {check['number']:>2}. {check['name']}")
        for detail in check["details"]:
            print(f"        {detail}")
        for failure in check["failures"]:
            print(f"     -> {failure}")
    print()
    print(f"findings ({len(report['findings'])})")
    for finding in report["findings"]:
        print(f"  [{finding['kind']:<14}] {finding['id']}  ({finding['status']})")
        print(f"      {finding['title']}")
    print()
    print(f"report: {path}")
    if report["passed"]:
        print()
        print("All ten §12 checks passed. The protocol works end to end on a real")
        print("contract. This is permission to consider the batch -- not to run it:")
        print("the 60-run batch still needs its own explicit opt-in.")
    else:
        print()
        print("STOP. §12: do not run the batch, and do not work around a protocol")
        print("failure by weakening the experimental design.")


if __name__ == "__main__":
    raise SystemExit(main())

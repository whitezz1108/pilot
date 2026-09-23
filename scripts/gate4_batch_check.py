"""Gate 4.5 §1 preflight and §3 post-run completeness, as two offline checks.

This script spends nothing and changes nothing. It reads the frozen inputs, the
plan and the raw tree, and answers two questions the batch depends on:

``preflight``
    Is the experiment about to be run the experiment that was frozen? Twelve
    checks: the registry holds twelve cases, the plan holds sixty unique jobs in
    the declared composition, the plan's fingerprint is the frozen one, the case
    selection and the policy still hash to their frozen digests, the prompt
    files still render to the reviewed text, the model configuration still
    resolves to the declared parameters, the credential *names* are set, the
    smoke cannot be mistaken for the batch, and no earlier batch output exists
    to resume over by accident.

``postrun``
    Did the batch that ran belong to one frozen version, and is every planned
    cell present exactly once? Sixty jobs, sixty completed runs, no missing
    cell, no duplicated cell, and -- the check that makes a resume safe -- every
    completed run carrying the frozen plan fingerprint, so a tree that mixes two
    versions of the experiment cannot be reported as one batch.

Neither phase is a design check. Both are here so that "the batch ran" is a
statement someone can verify rather than one they have to trust.

    .venv/Scripts/python.exe scripts/gate4_batch_check.py preflight
    .venv/Scripts/python.exe scripts/gate4_batch_check.py postrun

Exit status is 0 when every check passes and 1 otherwise. A failed preflight
means do not spend the runs; a failed postrun means do not read the numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_conditions_v1, load_models_v1, load_policy_v1  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.development import build_gate4_development_set  # noqa: E402
from pilot01.experiment.layout import ExperimentPaths  # noqa: E402
from pilot01.experiment.runspec import RunPlan  # noqa: E402

EXPERIMENT_ID = "gate4-development"
SMOKE_EXPERIMENT_ID = "gate4-smoke"

REGISTRY_PATH = REPO_ROOT / "outputs" / "development" / "cases_v1.json"
CASE_SET_PATH = REPO_ROOT / "outputs" / "development" / "development_cases_v1.json"
SELECTION_AUDIT_PATH = REPO_ROOT / "outputs" / "development" / "selection_audit.json"
PROMPT_REVIEW_PATH = REPO_ROOT / "outputs" / "development" / "prompt_review.json"
PLAN_PATH = REPO_ROOT / "outputs" / "manifests" / EXPERIMENT_ID / "run_plan.json"

PROMPT_FILES = (
    REPO_ROOT / "prompts" / "manager_v1.md",
    REPO_ROOT / "prompts" / "compliance_v1.md",
    REPO_ROOT / "prompts" / "repair_v1.md",
)

FROZEN_PLAN_FINGERPRINT = (
    "sha256:294287c6c33055c2058c71fd6352163b9a6f941700e0364428bf30c78c2f5019"
)
FROZEN_SELECTION_FINGERPRINT = (
    "sha256:f24db8fc67c2e681c9cca9736a9856c047a71993cadb6d8a3b2ee2b62383e897"
)
FROZEN_POLICY_FINGERPRINT = (
    "sha256:d507013815d96a97021d5fe8412d44335e64acfe054e75475d6ce1dd55c0005f"
)

EXPECTED_CASES = 12
EXPECTED_POSITIVE_CASES = 8
EXPECTED_SENTINEL_CASES = 4
EXPECTED_JOBS = 60
EXPECTED_POSITIVE_JOBS = 48
EXPECTED_SENTINEL_JOBS = 12
EXPECTED_BY_ARM = {"E0": 36, "E1": 24}
EXPECTED_BY_CONDITION = {"A0V0": 20, "A1V0": 20, "A1V1": 20}
EXPECTED_CONDITIONS = ("A0V0", "A1V0", "A1V1")
EXPECTED_MODEL_ID = "DeepSeek-V4.1-Flash"

# §1.9: the parameters the frozen plan's model fingerprint was computed over.
EXPECTED_MODEL_PARAMS = {
    "temperature": 0.0,
    "top_p": None,
    "max_output_tokens": 4096,
    "timeout_seconds": 180.0,
}


class Check:
    """One numbered check, its evidence, and its failures."""

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


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def preflight() -> dict:
    checks = [
        _check_registry(),
        _check_plan(),
        _check_composition(),
        _check_duplicates(),
        _check_plan_fingerprint(),
        _check_selection_fingerprint(),
        _check_policy_fingerprint(),
        _check_prompt_freeze(),
        _check_model_config(),
        _check_credentials(),
        _check_smoke_isolation(),
        _check_existing_batch_output(),
    ]
    return _report("preflight", EXPERIMENT_ID, checks)


def _check_registry() -> Check:
    check = Check(1, "registry exists and holds exactly twelve cases")
    if not REGISTRY_PATH.exists():
        check.fail(f"{REGISTRY_PATH} does not exist")
        return check
    registry = CaseRegistry.load_json(str(REGISTRY_PATH))
    check.note(f"{REGISTRY_PATH.relative_to(REPO_ROOT)} ({_sha256_file(REGISTRY_PATH)})")
    if not check.require(
        len(registry.case_ids) == EXPECTED_CASES,
        f"{len(registry.case_ids)} case(s)",
        f"{len(registry.case_ids)} case(s), expected {EXPECTED_CASES}",
    ):
        return check
    positives = [c for c in registry.case_ids if not registry.get(c).is_negative_sentinel]
    sentinels = [c for c in registry.case_ids if registry.get(c).is_negative_sentinel]
    check.require(
        len(positives) == EXPECTED_POSITIVE_CASES,
        f"{len(positives)} positive case(s)",
        f"{len(positives)} positive case(s), expected {EXPECTED_POSITIVE_CASES}",
    )
    check.require(
        len(sentinels) == EXPECTED_SENTINEL_CASES,
        f"{len(sentinels)} sentinel case(s)",
        f"{len(sentinels)} sentinel case(s), expected {EXPECTED_SENTINEL_CASES}",
    )
    # §1.1's clause categories, read from the cases rather than asserted.
    categories = sorted({registry.get(c).target_category for c in positives})
    check.require(
        categories == ["Change Of Control", "Termination For Convenience"],
        f"positive-case categories: {categories}",
        f"positive-case categories {categories} are not the two declared ones",
    )
    # A sentinel must offer E0 only: an omission arm on a case with nothing to
    # omit would be a cell with no treatment in it.
    bad = [
        c
        for c in sentinels
        if sorted(e.value for e in registry.get(c).error_conditions) != ["E0"]
    ]
    check.require(
        not bad,
        "every sentinel declares E0 only",
        f"sentinel(s) with an arm other than E0 alone: {bad}",
    )
    check.note(
        "cases: " + ", ".join(registry.case_ids)
    )
    return check


def _check_plan() -> Check:
    check = Check(2, "run plan exists and holds exactly sixty unique jobs")
    if not PLAN_PATH.exists():
        check.fail(f"{PLAN_PATH} does not exist")
        return check
    plan = RunPlan.load_json(PLAN_PATH)
    check.note(f"{PLAN_PATH.relative_to(REPO_ROOT)}")
    check.require(
        len(plan.jobs) == EXPECTED_JOBS,
        f"{len(plan.jobs)} job(s)",
        f"{len(plan.jobs)} job(s), expected {EXPECTED_JOBS}",
    )
    check.require(
        plan.experiment_id == EXPERIMENT_ID,
        f"experiment_id {plan.experiment_id!r}",
        f"experiment_id {plan.experiment_id!r}, expected {EXPERIMENT_ID!r}",
    )
    check.require(
        plan.repeat_count == 1,
        "repeat_count 1 (one repeat only, as declared)",
        f"repeat_count {plan.repeat_count}, expected 1",
    )
    check.require(
        tuple(plan.condition_ids) == EXPECTED_CONDITIONS,
        f"conditions {list(plan.condition_ids)}",
        f"conditions {list(plan.condition_ids)}, expected {list(EXPECTED_CONDITIONS)}",
    )
    return check


def _check_composition() -> Check:
    check = Check(3, "plan composition matches the frozen design")
    plan = RunPlan.load_json(PLAN_PATH)
    registry = CaseRegistry.load_json(str(REGISTRY_PATH))

    def is_sentinel(job) -> bool:
        return registry.get(job.case_id).is_negative_sentinel

    positives = [job for job in plan.jobs if not is_sentinel(job)]
    sentinels = [job for job in plan.jobs if is_sentinel(job)]
    arms = Counter(job.error_condition.value for job in plan.jobs)
    conditions = Counter(job.condition_id for job in plan.jobs)

    check.require(
        len(positives) == EXPECTED_POSITIVE_JOBS,
        f"positive jobs: {len(positives)} (8 cases x 2 arms x 3 conditions)",
        f"positive jobs: {len(positives)}, expected {EXPECTED_POSITIVE_JOBS}",
    )
    check.require(
        len(sentinels) == EXPECTED_SENTINEL_JOBS,
        f"sentinel jobs: {len(sentinels)} (4 cases x E0 x 3 conditions)",
        f"sentinel jobs: {len(sentinels)}, expected {EXPECTED_SENTINEL_JOBS}",
    )
    for arm, expected in EXPECTED_BY_ARM.items():
        check.require(
            arms.get(arm, 0) == expected,
            f"{arm} = {arms.get(arm, 0)}",
            f"{arm} = {arms.get(arm, 0)}, expected {expected}",
        )
    for condition, expected in EXPECTED_BY_CONDITION.items():
        check.require(
            conditions.get(condition, 0) == expected,
            f"{condition} = {conditions.get(condition, 0)}",
            f"{condition} = {conditions.get(condition, 0)}, expected {expected}",
        )
    check.require(
        not [job for job in sentinels if job.error_condition.value != "E0"],
        "every sentinel job is E0",
        "a sentinel job carries an arm other than E0",
    )
    # Governance flags must follow from the condition, not from the job's name.
    wrong = [
        job.run_id
        for job in plan.jobs
        if job.source_access != (job.condition_id != "A0V0")
        or job.verification_required != (job.condition_id == "A1V1")
    ]
    check.require(
        not wrong,
        "source_access and verification_required follow the condition everywhere",
        f"job(s) whose governance flags disagree with their condition: {wrong}",
    )
    return check


def _check_duplicates() -> Check:
    check = Check(4, "no duplicated job id or run id")
    plan = RunPlan.load_json(PLAN_PATH)
    run_ids = Counter(job.run_id for job in plan.jobs)
    duplicated_runs = sorted(rid for rid, n in run_ids.items() if n > 1)
    check.require(
        not duplicated_runs,
        f"{len(run_ids)} distinct run id(s) across {len(plan.jobs)} job(s)",
        f"duplicated run id(s): {duplicated_runs}",
    )
    cells = Counter(job.cell for job in plan.jobs)
    duplicated_cells = sorted(str(cell) for cell, n in cells.items() if n > 1)
    check.require(
        not duplicated_cells,
        f"{len(cells)} distinct (case, arm, condition, repeat) cell(s)",
        f"cell(s) occupied more than once: {duplicated_cells}",
    )
    missing = plan.missing_cells(CaseRegistry.load_json(str(REGISTRY_PATH)))
    check.require(
        not missing,
        "every expected cell is occupied",
        f"{len(missing)} expected cell(s) unoccupied: {[str(c) for c in missing[:5]]}",
    )
    return check


def _check_plan_fingerprint() -> Check:
    check = Check(5, "plan fingerprint is the frozen Gate 4 value")
    plan = RunPlan.load_json(PLAN_PATH)
    observed = plan.fingerprint()
    check.note(f"observed {observed}")
    check.note(f"frozen   {FROZEN_PLAN_FINGERPRINT}")
    check.require(
        observed == FROZEN_PLAN_FINGERPRINT,
        "the plan hashes to the frozen digest, order included",
        "the plan does NOT hash to the frozen digest: this is not the frozen "
        "experiment, and nothing may be run against it until that is explained",
    )
    return check


def _check_selection_fingerprint() -> Check:
    check = Check(6, "case selection fingerprint is the frozen Gate 4 value")
    payload = _load_json(CASE_SET_PATH)
    stored = payload["selection"]["fingerprint"]
    check.note(f"stored in development_cases_v1.json: {stored}")
    check.require(
        stored == FROZEN_SELECTION_FINGERPRINT,
        "the stored selection digest is the frozen one",
        f"stored selection digest {stored} is not the frozen one",
    )
    audit = _load_json(SELECTION_AUDIT_PATH)["fingerprint"]
    check.require(
        audit == FROZEN_SELECTION_FINGERPRINT,
        "the selection audit agrees with the frozen digest",
        f"selection audit digest {audit} disagrees with the frozen one",
    )
    # The strongest form: rebuild the set offline from the frozen CUAD
    # annotations and confirm the same twelve cases hash to the same digest. A
    # frozen file that no longer reproduces is a file, not a freeze.
    build = build_gate4_development_set()
    rebuilt = build.case_set.selection.fingerprint
    check.require(
        rebuilt == FROZEN_SELECTION_FINGERPRINT,
        "an offline rebuild of the selection reproduces the frozen digest",
        f"an offline rebuild produced {rebuilt}, which is not the frozen digest",
    )
    rebuilt_ids = list(build.registry.case_ids)
    stored_ids = [case["case_id"] for case in payload["cases"]]
    check.require(
        rebuilt_ids == stored_ids,
        f"the rebuild selects the same twelve cases in the same order",
        f"the rebuild selects {rebuilt_ids}, the frozen file holds {stored_ids}",
    )
    return check


def _check_policy_fingerprint() -> Check:
    check = Check(7, "POLICY-01 fingerprint is the frozen Gate 4 value")
    payload = _load_json(CASE_SET_PATH)
    stored = payload["policy"]["fingerprint"]
    check.note(f"stored in development_cases_v1.json: {stored}")
    check.require(
        stored == FROZEN_POLICY_FINGERPRINT,
        "the stored policy digest is the frozen one",
        f"stored policy digest {stored} is not the frozen one",
    )
    live = load_policy_v1().fingerprint()
    check.require(
        live == FROZEN_POLICY_FINGERPRINT,
        "config/policies/policy_v1.yaml still hashes to the frozen digest",
        f"config/policies/policy_v1.yaml hashes to {live}, not the frozen digest: "
        "the rule under test has moved",
    )
    check.note(
        "policy: "
        f"{payload['policy']['policy_id']} v{payload['policy']['policy_version']} "
        f"({payload['policy']['policy_status']}), mapping "
        f"{payload['policy']['clause_status_mapping']}"
    )
    return check


def _check_prompt_freeze() -> Check:
    check = Check(8, "manager and compliance prompts are unchanged since the §10 review")
    for path in PROMPT_FILES:
        if not path.exists():
            check.fail(f"{path.relative_to(REPO_ROOT)} is missing")
            continue
        check.note(f"{path.relative_to(REPO_ROOT)}  {_sha256_file(path)}")
    if not check.passed:
        return check

    stored = _load_json(PROMPT_REVIEW_PATH)
    # The review renders every prompt the batch will send. Re-running it offline
    # and comparing the payload byte for byte is what proves the *rendered*
    # text is unchanged -- a file hash alone would not, because the rendering
    # also depends on the registry, the conditions and the views.
    spec = importlib.util.spec_from_file_location(
        "review_prompts_gate4", REPO_ROOT / "scripts" / "review_prompts_gate4.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = build_gate4_development_set()
    again = module.review(build.registry, build.annotations)
    check.require(
        again == stored,
        f"re-rendering all {stored['rendered_prompt_count']} prompts reproduces "
        f"prompt_review.json exactly ({len(stored['checks'])} checks, "
        f"passed={stored['passed']})",
        "re-rendering the prompts does NOT reproduce the reviewed payload: the "
        "prompt text or the renderer has moved since the §10 review",
    )
    check.require(
        stored["passed"] and stored["prompt_files_unchanged_by_this_review"],
        "the §10 review passed and modified no prompt file",
        "the §10 review did not pass, or it modified a prompt file",
    )
    return check


def _check_model_config() -> Check:
    check = Check(9, "model configuration still resolves to the declared parameters")
    models = load_models_v1()
    for role, config in sorted(models.roles.items()):
        check.note(f"{role}: {config.model_id} ({config.fingerprint()})")
        check.require(
            config.model_id == EXPECTED_MODEL_ID,
            f"{role} model_id is {config.model_id}",
            f"{role} model_id is {config.model_id!r}, expected {EXPECTED_MODEL_ID!r}",
        )
        for field, expected in EXPECTED_MODEL_PARAMS.items():
            observed = getattr(config, field)
            check.require(
                observed == expected,
                f"{role} {field} = {observed}",
                f"{role} {field} = {observed!r}, expected {expected!r}",
            )
    # The plan carries the fingerprint the runs will be stamped with; a config
    # that resolves differently from the plan's own record would mean the batch
    # and its manifest describe two different experiments.
    plan = RunPlan.load_json(PLAN_PATH)
    from pilot01.experiment.runspec import model_fingerprint

    check.require(
        model_fingerprint(models) == plan.model_fingerprint,
        "the live config fingerprint equals the plan's model fingerprint",
        "the live config fingerprint differs from the plan's model fingerprint",
    )
    return check


def _check_credentials() -> Check:
    check = Check(10, "API credentials are present (names only, never values)")
    from pilot01.model.openai_compat import missing_credentials

    missing = missing_credentials()
    check.require(
        not missing,
        "both credential variables are set in this environment",
        "credential variable(s) not set: "
        + ", ".join(missing)
        + ". This is an operational environment issue, not an experimental one; "
        "the batch cannot be launched until they are present, and 'run --live' "
        "refuses without them.",
    )
    check.note("no credential value was read, printed, hashed or written by this check")
    return check


def _check_smoke_isolation() -> Check:
    check = Check(11, "smoke output cannot be confused with the batch output")
    batch_paths = ExperimentPaths(root=REPO_ROOT / "outputs", experiment_id=EXPERIMENT_ID)
    smoke_paths = ExperimentPaths(
        root=REPO_ROOT / "outputs", experiment_id=SMOKE_EXPERIMENT_ID
    )
    check.require(
        batch_paths.raw_dir != smoke_paths.raw_dir,
        f"the two experiments live in different trees: "
        f"{batch_paths.raw_dir.relative_to(REPO_ROOT)} vs "
        f"{smoke_paths.raw_dir.relative_to(REPO_ROOT)}",
        "the smoke and the batch would share a raw directory",
    )
    smoke_runs = (
        sorted(p.name for p in smoke_paths.raw_dir.iterdir() if p.is_dir())
        if smoke_paths.raw_dir.exists()
        else []
    )
    check.require(
        all(name.startswith(SMOKE_EXPERIMENT_ID + "__") for name in smoke_runs),
        f"all {len(smoke_runs)} smoke run id(s) are namespaced {SMOKE_EXPERIMENT_ID}__",
        "a smoke run directory is not namespaced to the smoke experiment",
    )
    check.require(
        not (batch_paths.raw_dir.exists() and any(batch_paths.raw_dir.iterdir())),
        "no batch run directory exists, so nothing can be misread as a batch result",
        "a batch raw directory already exists and is not empty",
    )
    check.note(
        f"smoke holds {len(smoke_runs)} run(s) under "
        f"{smoke_paths.raw_dir.relative_to(REPO_ROOT)}; the batch writes under "
        f"{batch_paths.raw_dir.relative_to(REPO_ROOT)}"
    )
    return check


def _check_existing_batch_output() -> Check:
    check = Check(12, "no earlier batch output exists, and resume is safe if it appears")
    paths = ExperimentPaths(root=REPO_ROOT / "outputs", experiment_id=EXPERIMENT_ID)
    if not paths.raw_dir.exists() or not any(paths.raw_dir.iterdir()):
        check.note(
            f"{paths.raw_dir.relative_to(REPO_ROOT)} does not exist: the batch has "
            "not been run, so it begins normally rather than resuming"
        )
    else:
        completed = [
            d.name for d in paths.raw_dir.iterdir() if (d / "run.json").exists()
        ]
        check.note(
            f"{paths.raw_dir.relative_to(REPO_ROOT)} holds {len(completed)} "
            "completed run(s): this would be a resume, and only the missing jobs "
            "would be bought"
        )
    # How the runner behaves, recorded so the safety claim is checkable rather
    # than asserted: a completed run is skipped under --resume and refused
    # without it; an interrupted directory is moved aside, never overwritten.
    check.note(
        "runner: --resume skips a job whose run.json exists and moves an "
        "interrupted attempt aside; without --resume a non-empty run directory "
        "raises instead of overwriting; --rerun-failed additionally moves a "
        "completed-but-failed attempt aside before re-running it"
    )
    check.note(
        "each run.json records the plan fingerprint it was produced under, so a "
        "tree mixing two versions of the plan is detectable after the fact -- "
        "the postrun phase checks exactly that"
    )
    return check


# ---------------------------------------------------------------------------
# post-run completeness
# ---------------------------------------------------------------------------


def postrun() -> dict:
    checks = [
        _check_completeness(),
        _check_cells_complete(),
        _check_one_frozen_version(),
    ]
    return _report("postrun", EXPERIMENT_ID, checks)


def _read_runs(paths: ExperimentPaths) -> dict[str, dict]:
    runs: dict[str, dict] = {}
    if not paths.raw_dir.exists():
        return runs
    for directory in sorted(paths.raw_dir.iterdir()):
        run_json = directory / "run.json"
        if not directory.is_dir() or not run_json.exists():
            continue
        runs[directory.name] = {
            "dir": directory,
            "artifact": json.loads(run_json.read_text(encoding="utf-8")),
        }
    return runs


def _check_completeness() -> Check:
    check = Check(1, "every planned job completed exactly once")
    plan = RunPlan.load_json(PLAN_PATH)
    paths = ExperimentPaths(root=REPO_ROOT / "outputs", experiment_id=EXPERIMENT_ID)
    runs = _read_runs(paths)
    planned = {job.run_id for job in plan.jobs}
    present = set(runs)
    check.note(f"planned {len(planned)}, completed {len(present)}")
    check.require(
        len(planned) == EXPECTED_JOBS,
        f"{len(planned)} planned job(s)",
        f"{len(planned)} planned job(s), expected {EXPECTED_JOBS}",
    )
    missing = sorted(planned - present)
    extra = sorted(present - planned)
    check.require(
        not missing,
        "no missing job",
        f"{len(missing)} missing job(s): {missing[:5]}",
    )
    check.require(
        not extra,
        "no unplanned run in the tree",
        f"{len(extra)} unplanned run(s): {extra[:5]}",
    )
    # A duplicated completed job cannot happen under the runner's own rules --
    # a second attempt at an occupied directory is refused -- so this is a check
    # on the tree rather than on the runner.
    duplicates = [
        name for name, count in Counter(present).items() if count > 1
    ]
    check.require(
        not duplicates,
        "no duplicated completed job",
        f"duplicated completed job(s): {duplicates}",
    )
    interrupted = [
        d.name
        for d in (paths.raw_dir.iterdir() if paths.raw_dir.exists() else [])
        if d.is_dir() and not (d / "run.json").exists()
    ]
    check.note(
        f"interrupted attempt(s) left in the tree: {len(interrupted)} "
        f"({interrupted[:5]})" if interrupted else "no interrupted attempt left in the tree"
    )
    return check


def _check_cells_complete() -> Check:
    check = Check(2, "every expected case x memo x condition cell exists exactly once")
    plan = RunPlan.load_json(PLAN_PATH)
    registry = CaseRegistry.load_json(str(REGISTRY_PATH))
    paths = ExperimentPaths(root=REPO_ROOT / "outputs", experiment_id=EXPERIMENT_ID)
    runs = _read_runs(paths)

    expected = set(plan.cells())
    observed = Counter(
        (
            artifact["case_id"],
            artifact["error_condition"],
            artifact["condition_id"],
            artifact["repeat_index"],
        )
        for artifact in (run["artifact"] for run in runs.values())
    )
    missing = sorted(expected - set(observed))
    duplicated = sorted(str(cell) for cell, n in observed.items() if n > 1)
    check.require(
        not missing,
        f"all {len(expected)} expected cell(s) present",
        f"{len(missing)} missing cell(s): {[str(c) for c in missing[:5]]}",
    )
    check.require(
        not duplicated,
        "no cell holds more than one run",
        f"cell(s) holding more than one run: {duplicated}",
    )
    sentinel_arms = Counter(
        artifact["error_condition"]
        for artifact in (run["artifact"] for run in runs.values())
        if registry.get(artifact["case_id"]).is_negative_sentinel
    )
    check.require(
        set(sentinel_arms) <= {"E0"},
        f"sentinel runs carry E0 only ({dict(sentinel_arms)})",
        f"a sentinel run carries an arm other than E0: {dict(sentinel_arms)}",
    )
    return check


def _check_one_frozen_version() -> Check:
    check = Check(3, "every completed run belongs to the one frozen version")
    plan = RunPlan.load_json(PLAN_PATH)
    paths = ExperimentPaths(root=REPO_ROOT / "outputs", experiment_id=EXPERIMENT_ID)
    runs = _read_runs(paths)
    fingerprints = Counter(
        run["artifact"].get("plan_fingerprint") for run in runs.values()
    )
    for fingerprint, count in sorted(fingerprints.items(), key=lambda kv: str(kv[0])):
        check.note(f"{count} run(s) under plan fingerprint {fingerprint}")
    check.require(
        set(fingerprints) == {plan.fingerprint()},
        "every run was produced under the frozen plan fingerprint",
        "the tree mixes plan fingerprints: the runs are not all the same "
        "experiment and must not be reported as one batch",
    )
    model_fingerprints = Counter(
        run["artifact"].get("model_fingerprint") for run in runs.values()
    )
    check.require(
        len(model_fingerprints) == 1 and model_fingerprints.get(plan.model_fingerprint) == len(runs),
        "every run was produced under the frozen model fingerprint",
        f"the tree mixes model fingerprints: {dict(model_fingerprints)}",
    )
    return check


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _report(phase: str, experiment_id: str, checks: list[Check]) -> dict:
    return {
        "gate": "gate-4.5",
        "phase": phase,
        "experiment_id": experiment_id,
        "checks": [check.as_dict() for check in checks],
        "passed": all(check.passed for check in checks),
    }


def _print_report(report: dict) -> None:
    print(f"Gate 4.5 {report['phase']} -- {report['experiment_id']}")
    print()
    for check in report["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"[{mark}] {check['number']:>2}. {check['name']}")
        for detail in check["details"]:
            print(f"        {detail}")
        for failure in check["failures"]:
            print(f"     -> {failure}")
    print()
    failed = [check["number"] for check in report["checks"] if not check["passed"]]
    if report["passed"]:
        print("all checks passed")
    else:
        print(f"check(s) failed: {failed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=("preflight", "postrun"))
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "outputs" / "development"))
    args = parser.parse_args(argv)

    report = preflight() if args.phase == "preflight" else postrun()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"gate4_{args.phase}_check.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    _print_report(report)
    print()
    print(f"report: {report_path.relative_to(REPO_ROOT)}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

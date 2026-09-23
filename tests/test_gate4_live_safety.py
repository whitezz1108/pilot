"""Gate 4 §13: what it takes to spend money, and what never reaches a file.

Gate 2 already proved the suite cannot reach the network. This file is about the
*operator's* path: the CLI that a person actually runs, and the three separate
conditions that must all hold before it sends anything -- ``--live``, a
credential in the environment, and an explicit opt-in to a batch beyond the
smoke size. Each is tested for being genuinely necessary, because a gate that
can be satisfied by accident is not a gate.

The other half is the credential itself: that it is read from the environment,
never printed, never serialized into a plan, registry, log or message, and never
substituted for by anything a test could supply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import gate4_fixture as g4
import pilot01
from pilot01.config import load_conditions_v1, load_models_v1
from pilot01.experiment.cli import (
    EXIT_OK,
    EXIT_USAGE,
    FULL_RUN_OPT_IN_ENV,
    FULL_RUN_OPT_IN_FLAG,
    LIVE_BATCH_JOB_THRESHOLD,
    build_parser,
    main,
)
from pilot01.experiment.runspec import build_run_plan
from pilot01.model.openai_compat import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL_ENV
from pilot01.schemas import ErrorCondition

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gate4_smoke import build_smoke_plan  # noqa: E402
PLAN_PATH = (
    REPO_ROOT / "outputs" / "manifests" / "gate4-development" / "run_plan.json"
)
REGISTRY_PATH = REPO_ROOT / "outputs" / "development" / "cases_v1.json"

FAKE_KEY = "sk-" + "gate4-live-safety-test-value-not-a-real-credential"


@pytest.fixture
def no_credentials(monkeypatch):
    """The environment as it is on a machine that has never been configured."""
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_BASE_URL_ENV, raising=False)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv(DEFAULT_BASE_URL_ENV, "https://example.invalid/v1")
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, FAKE_KEY)


@pytest.fixture
def no_opt_in(monkeypatch):
    monkeypatch.delenv(FULL_RUN_OPT_IN_ENV, raising=False)


@pytest.fixture(scope="module")
def sixty_job_plan():
    return build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
    )


def _run_cli(argv: list[str]) -> int:
    """Drive the CLI in-process, the way the operator's shell would."""
    return main(argv)


# ---------------------------------------------------------------------------
# §13: the three conditions
# ---------------------------------------------------------------------------


def test_the_full_plan_is_sixty_jobs_and_the_smoke_threshold_is_six(sixty_job_plan):
    assert len(sixty_job_plan.jobs) == 60
    assert LIVE_BATCH_JOB_THRESHOLD == 6


def test_a_live_batch_over_the_threshold_is_refused_without_the_opt_in(
    credentials, no_opt_in, tmp_path, capsys
):
    """§13: credentials existing is not a decision to spend them."""
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
        ]
    )
    assert code == EXIT_USAGE
    message = capsys.readouterr().err
    assert "60 jobs" in message
    assert FULL_RUN_OPT_IN_FLAG in message
    assert FULL_RUN_OPT_IN_ENV in message
    assert "Nothing was sent" in message


def _assert_the_batch_gate_opened(argv: list[str], capsys) -> None:
    """Drive the CLI and confirm the refusal was *not* the batch gate.

    With all three conditions met the gate opens and the run proceeds far enough
    to attempt a connection, which the suite's autouse network guard then
    refuses. That refusal is the proof the gate opened -- reaching the network is
    exactly what the gate exists to prevent, so a run that gets there has passed
    it. Any other exception is a real failure and propagates.
    """
    try:
        _run_cli(argv)
    except AssertionError as exc:
        assert "network connection" in str(exc), exc
    assert "more than the" not in capsys.readouterr().err


def test_the_opt_in_flag_alone_is_enough_to_get_past_the_gate(
    credentials, no_opt_in, tmp_path, capsys
):
    _assert_the_batch_gate_opened(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
            FULL_RUN_OPT_IN_FLAG,
        ],
        capsys,
    )


def test_the_environment_opt_in_alone_is_also_enough(credentials, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(FULL_RUN_OPT_IN_ENV, "1")
    _assert_the_batch_gate_opened(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
        ],
        capsys,
    )


def test_an_opt_in_value_other_than_one_is_not_an_opt_in(
    credentials, tmp_path, monkeypatch, capsys
):
    """A variable set to "0", "false" or "" must not read as consent."""
    for value in ("0", "false", "no", "", "true", "yes"):
        monkeypatch.setenv(FULL_RUN_OPT_IN_ENV, value)
        code = _run_cli(
            [
                "run",
                "--registry",
                str(REGISTRY_PATH),
                "--plan",
                str(PLAN_PATH),
                "--out",
                str(tmp_path),
                "--live",
            ]
        )
        assert code == EXIT_USAGE, f"{value!r} was read as an opt-in"
        capsys.readouterr()


def test_the_opt_in_is_not_a_credential_and_does_not_replace_one(
    no_credentials, tmp_path, monkeypatch, capsys
):
    """Opting in to a batch must not stand in for the key."""
    monkeypatch.setenv(FULL_RUN_OPT_IN_ENV, "1")
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
        ]
    )
    assert code == EXIT_USAGE
    message = capsys.readouterr().err
    assert DEFAULT_API_KEY_ENV in message
    assert DEFAULT_BASE_URL_ENV in message


def test_missing_credentials_fail_before_the_batch_gate_and_say_what_is_missing(
    no_credentials, no_opt_in, tmp_path, capsys
):
    """The credential message must not be masked by the opt-in message."""
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
        ]
    )
    assert code == EXIT_USAGE
    message = capsys.readouterr().err
    assert "needs" in message
    assert DEFAULT_API_KEY_ENV in message
    assert "60 jobs" not in message


def test_the_smoke_size_plan_does_not_need_the_second_opt_in(
    credentials, no_opt_in, tmp_path, capsys
):
    """§12's smoke sits exactly at the threshold, by construction."""
    smoke = build_smoke_plan(registry=g4.registry())
    assert len(smoke.jobs) == LIVE_BATCH_JOB_THRESHOLD == 6
    path = tmp_path / "smoke_plan.json"
    smoke.write_json(path)
    _assert_the_batch_gate_opened(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(path),
            "--out",
            str(tmp_path / "out"),
            "--live",
        ],
        capsys,
    )


def test_the_smoke_covers_the_cells_the_spec_names_first():
    """§12's four cells come first, in its order; the completing two follow."""
    smoke = build_smoke_plan(registry=g4.registry())
    cells = [(job.error_condition.value, job.condition_id) for job in smoke.jobs]
    assert cells[:4] == [
        ("E0", "A0V0"),
        ("E1", "A0V0"),
        ("E1", "A1V0"),
        ("E1", "A1V1"),
    ]


def test_the_smoke_plan_is_complete_for_its_case():
    """Why the smoke is six and not four.

    ``RunPlan.check`` is what refused a four-cell plan, and it is right to: a
    plan that covers a strict subset of its own grid is how an experiment
    silently skips cells. The smoke answers that by covering the whole grid for
    one case rather than by asking the runner to look away -- so the check that
    protects the batch is still doing its job here.
    """
    smoke = build_smoke_plan(registry=g4.registry())
    conditions = load_conditions_v1()
    smoke.check(conditions, g4.registry())  # raises if incomplete

    arms = {job.error_condition for job in smoke.jobs}
    governance = {job.condition_id for job in smoke.jobs}
    assert arms == set(ErrorCondition)
    assert governance == set(conditions.main_conditions)
    assert len(smoke.jobs) == len(arms) * len(governance)


def test_the_smoke_uses_one_real_positive_case_and_no_sentinel():
    smoke = build_smoke_plan(registry=g4.registry())
    assert smoke.case_ids == ("DEV-POS-COC-0496",)
    assert smoke.case_ids[0] in g4.POSITIVE_CASE_IDS


def test_the_smoke_jobs_are_identical_to_the_batch_jobs_they_stand_in_for():
    """A smoke that ran a different job would be evidence about a run nobody makes."""
    smoke = build_smoke_plan(registry=g4.registry())
    batch = build_run_plan(
        experiment_id="gate4-smoke",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
        case_ids=["DEV-POS-COC-0496"],
    )
    smoke_by_cell = {job.cell: job for job in smoke.jobs}
    for job in batch.jobs:
        if job.cell in smoke_by_cell:
            assert smoke_by_cell[job.cell] == job


def test_the_smoke_plan_carries_no_gold_and_no_credential():
    smoke = build_smoke_plan(registry=g4.registry())
    serialized = smoke.model_dump_json().lower()
    for forbidden in ("gold", "api_key", "bearer", "secret", "evidence"):
        assert forbidden not in serialized


def test_a_case_that_cannot_supply_every_smoke_cell_is_refused():
    """A sentinel has no E1, so it cannot be smoked with this script."""
    with pytest.raises(SystemExit, match="cannot supply every smoke cell"):
        build_smoke_plan(registry=g4.registry(), case_id=g4.SENTINEL_CASE_IDS[0])


def test_a_scripted_run_never_needs_credentials_or_an_opt_in(no_credentials, no_opt_in, tmp_path):
    """§17: the suite must be able to drive the full plan offline.

    No credential, no opt-in, no --live: a scripted transport is a different
    thing entirely and must not be gated behind either.
    """
    script = tmp_path / "responses.json"
    script.write_text(json.dumps({}), encoding="utf-8")
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path / "out"),
            "--responses",
            str(script),
        ]
    )
    assert code != EXIT_USAGE


def test_live_and_scripted_transports_cannot_be_combined(credentials, tmp_path):
    script = tmp_path / "responses.json"
    script.write_text(json.dumps({}), encoding="utf-8")
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
            "--responses",
            str(script),
        ]
    )
    assert code == EXIT_USAGE


def test_a_run_with_no_transport_is_refused(credentials, tmp_path, capsys):
    """There is no default, because the default would be the one that spends."""
    code = _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
        ]
    )
    assert code == EXIT_USAGE
    assert "--responses" in capsys.readouterr().err


def test_the_opt_in_flag_is_not_accepted_by_any_other_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["score", "--registry", "r.json", "--experiment-id", "x", FULL_RUN_OPT_IN_FLAG])


def test_the_threshold_is_a_size_limit_and_not_a_case_count():
    """Documented as a size so it stays correct if the smoke's shape changes."""
    import inspect

    source = inspect.getsource(
        sys.modules["pilot01.experiment.cli"]
    )
    assert "LIVE_BATCH_JOB_THRESHOLD = 6" in source
    assert "len(plan.jobs) > LIVE_BATCH_JOB_THRESHOLD" in source


def test_the_threshold_matches_the_smoke_it_is_named_after():
    """The constant's meaning is "no bigger than the smoke".

    That sentence is only true while the two numbers agree, and they are set in
    two different files -- so the agreement is a test rather than a comment.
    """
    assert LIVE_BATCH_JOB_THRESHOLD == len(build_smoke_plan(registry=g4.registry()).jobs)
    assert len(build_run_plan(
        experiment_id="gate4-development",
        registry=g4.registry(),
        conditions=load_conditions_v1(),
        repeat_count=1,
        seed=0,
        models=load_models_v1(),
    ).jobs) > LIVE_BATCH_JOB_THRESHOLD


# ---------------------------------------------------------------------------
# §13: the credential never lands anywhere
# ---------------------------------------------------------------------------


def test_the_credential_is_read_only_from_the_environment(credentials):
    """The adapter's own reader sees what the environment holds, by name."""
    from pilot01.model.openai_compat import missing_credentials

    assert missing_credentials() == ()


def test_missing_credentials_reports_names_and_never_values(no_credentials):
    from pilot01.model.openai_compat import missing_credentials

    missing = missing_credentials()
    assert set(missing) == {DEFAULT_BASE_URL_ENV, DEFAULT_API_KEY_ENV}


def test_no_artifact_written_by_the_build_contains_the_credential(credentials):
    """§13: no secret in logs, plans, registries or review packs."""
    for path in (REPO_ROOT / "outputs").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        assert FAKE_KEY not in text, path
        assert "PILOT01_API_KEY=" not in text, path


def test_the_gate4_files_this_gate_added_contain_no_credential_shape():
    """Gate 2 scans the whole repository; this re-checks the Gate-4 additions.

    Scoped to the files Gate 4 added, because a repo-wide scan would fail on
    Gate 2's own deliberately fake ``sk-unit-...`` fixtures -- placeholders that
    exist to prove the redactor works, and which the Gate-2 pattern set already
    knows to be fakes.
    """
    import re

    patterns = (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
        re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    )
    added = [
        REPO_ROOT / "tests" / "test_gate4_case_building.py",
        REPO_ROOT / "tests" / "test_gate4_memo_pairs.py",
        REPO_ROOT / "tests" / "test_gate4_policy.py",
        REPO_ROOT / "tests" / "test_gate4_plan.py",
        REPO_ROOT / "tests" / "test_gate4_live_safety.py",
        REPO_ROOT / "tests" / "gate4_fixture.py",
        REPO_ROOT / "scripts" / "gate4_smoke.py",
        REPO_ROOT / "scripts" / "build_development_cases.py",
    ]
    for path in added:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue
            # This file's own fake key is assembled from parts for exactly this
            # reason; anything else is a real leak.
            assert path.name == Path(__file__).name, f"{path}: {match.group()[:8]}..."


def test_the_registry_and_plan_hold_no_credential():
    """Scanned for credential *shapes*, not for words.

    A registry embeds whole contracts, and real contract text contains
    "authorization" and "bearer" as ordinary English ("marketing
    authorization", "bearer of the note"). A word scan would fail on the corpus
    rather than on a leak, so what is checked is what a leak would actually look
    like: a key name, a key assignment, or a key-shaped value.
    """
    import re

    key_names = ("api_key", "apikey", "access_token", "client_secret", "pilot01_api_key")
    key_values = (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
        re.compile(r"\bAKIA[0-9A-Z]{12,}"),
        re.compile(r"PILOT01_API_KEY\s*[:=]"),
    )
    for path in (REGISTRY_PATH, PLAN_PATH):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in key_names:
            assert forbidden not in lowered, path
        for pattern in key_values:
            assert pattern.search(text) is None, path


def test_the_cli_never_prints_the_credential(credentials, no_opt_in, tmp_path, capsys):
    _run_cli(
        [
            "run",
            "--registry",
            str(REGISTRY_PATH),
            "--plan",
            str(PLAN_PATH),
            "--out",
            str(tmp_path),
            "--live",
        ]
    )
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err


def test_the_opt_in_does_not_appear_in_the_plan_fingerprint(sixty_job_plan):
    """An opt-in is an operator action, not an experimental variable."""
    fingerprint = sixty_job_plan.fingerprint()
    assert FULL_RUN_OPT_IN_ENV.lower() not in fingerprint.lower()
    assert "live" not in fingerprint.lower()


# ---------------------------------------------------------------------------
# §17: pytest can never place a paid call
# ---------------------------------------------------------------------------


def test_the_suite_runs_offline_even_with_credentials_present(credentials):
    """The credentials exist here, and the guard still refuses every connection.

    This is the load-bearing property: a developer machine with a key exported
    must not be able to turn ``pytest`` into a bill.
    """
    import socket

    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("example.invalid", 443))


def test_no_gate4_test_can_place_a_paid_call():
    """§17: a Gate-4 test must not be able to reach the network.

    The rule is the *property*, not the name. Gate 4 originally forbade the
    Gate-4 files from naming the live adapter at all, which was true to its
    intent while no Gate-4 test needed one. Then the smoke found a truncation
    bug in the adapter itself -- it was dropping ``finish_reason`` -- and the
    fix needs a test, and that test needs the adapter.

    So the guard now says what it always meant: a Gate-4 test may construct the
    adapter only with an injected ``transport``, which replaces the module's
    only network call site. That is strictly stronger than Gate 2's own rule,
    which permits a *transport-less* client whose real transport would dial out
    if anything ever called it -- see ``ALLOWED_WITHOUT_TRANSPORT`` below.

    A construction without ``transport=`` is still refused, here and everywhere
    in the Gate-4 files.
    """
    import ast

    constructions = 0
    for path in (REPO_ROOT / "tests").glob("test_gate4_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "OpenAICompatibleClient":
                continue
            constructions += 1
            keywords = {keyword.arg for keyword in node.keywords}
            assert "transport" in keywords, (
                f"{path.name}:{node.lineno} constructs the live adapter without "
                "an injected transport, so it would use the real one"
            )
    # The guard is vacuous if nothing constructs it, so the count is asserted
    # rather than left implicit: a rename that made this loop find nothing would
    # otherwise turn the whole test into a tautology.
    assert constructions >= 1, (
        "no Gate-4 test constructs the adapter any more, so this guard proves "
        "nothing; remove it or point it at whatever replaced the adapter"
    )


ALLOWED_WITHOUT_TRANSPORT = {
    "test_network_guard.py",
    "test_model_client.py",
}
"""Gate 2's two documented exceptions.

Both exercise the adapter's *own* boundary -- one proves the runtime guard
blocks the live path, the other asserts the adapter's error messages -- and
neither calls ``complete()``. Anywhere else, a transport-less client is a call
site that could become a paid call, so it is refused here.
"""


def test_no_other_test_module_builds_a_client_without_a_transport():
    """Gate 2's rule, re-asserted here so a Gate-4 regression trips it too."""
    import ast

    for path in (REPO_ROOT / "tests").glob("test_*.py"):
        if path.name in ALLOWED_WITHOUT_TRANSPORT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "OpenAICompatibleClient":
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            assert "transport" in keywords, (
                f"{path.name}:{node.lineno} builds a client with no injected "
                "transport"
            )


def test_the_gate4_build_script_makes_no_model_call():
    """The offline build must be runnable on a machine with no credentials."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_development_cases.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, DEFAULT_API_KEY_ENV: "", DEFAULT_BASE_URL_ENV: ""},
    )
    assert result.returncode == 0
    assert "--live" not in result.stdout

"""§12: the entry point, and the safety behaviour it must have.

The CLI's job is to be boring and explicit. Two properties matter more than the
rest and get most of the attention here:

**No default spends money.** ``run`` has no default transport. Omitting both
``--responses`` and ``--live`` is a usage error, not a silent live run.

**A missing credential fails before the first paid call, naming the variable and
never its value.** The test asserts the *absence* of the secret from the output,
which is the half of that requirement a careless implementation loses.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

import pytest

import fixture_registry as fixture
from pilot01.experiment import ExperimentPaths, RunDirectory
from pilot01.experiment.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, build_parser, main


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry_path(tmp_path_factory):
    """The registry as the CLI sees it: a JSON file on disk.

    The CLI takes a path, not an object, so every test here goes through the
    same serialization the operator would.
    """
    path = tmp_path_factory.mktemp("cli") / "registry.json"
    _registry().write_json(path)
    return path


def run_cli(*argv, capsys=None):
    """Invoke ``main`` and return (exit code, stdout, stderr)."""
    code = main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


def test_every_command_is_reachable():
    """`run` addresses a plan rather than an experiment id, so it is built apart."""
    parser = build_parser()
    for command in ("plan", "score", "summary"):
        args = parser.parse_args(
            [command, "--registry", "r.json", "--experiment-id", "E"]
        )
        assert args.command == command

    args = parser.parse_args(
        ["run", "--registry", "r.json", "--plan", "p.json", "--responses", "x.json"]
    )
    assert args.command == "run"
    assert args.live is False


def test_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([])
    assert excinfo.value.code == EXIT_USAGE


def test_the_registry_is_required_by_every_command(capsys):
    for command in ("plan", "run", "score", "summary"):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args([command])
        assert excinfo.value.code == EXIT_USAGE


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def test_plan_writes_a_plan_and_a_csv(registry_path, tmp_path, capsys):
    out = tmp_path / "outputs"
    code, stdout, stderr = run_cli(
        "plan",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        "--seed", 3,
        "--conditions", "A1V1,A1V0",
        "--cases", fixture.CASE_A,
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    paths = ExperimentPaths(root=out, experiment_id="EXP-CLI")
    assert paths.run_plan_json.exists()
    assert paths.run_plan_csv.exists()

    payload = json.loads(paths.run_plan_json.read_text(encoding="utf-8"))
    assert payload["experiment_id"] == "EXP-CLI"
    assert payload["seed"] == 3
    assert payload["condition_ids"] == ["A1V1", "A1V0"]
    assert len(payload["jobs"]) == 4  # one case, two conditions, two arms
    assert "planned 4 run(s)" in stdout
    assert "fingerprint:" in stdout


def test_plan_narrows_to_a_named_condition(registry_path, tmp_path, capsys):
    out = tmp_path / "outputs"
    code, stdout, _ = run_cli(
        "plan",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        "--conditions", "A1V1",
        "--cases", f"{fixture.CASE_A},{fixture.CASE_B}",
        capsys=capsys,
    )

    assert code == EXIT_OK
    payload = json.loads(
        ExperimentPaths(root=out, experiment_id="EXP-CLI").run_plan_json.read_text(
            encoding="utf-8"
        )
    )
    assert payload["condition_ids"] == ["A1V1"]
    assert {job["condition_id"] for job in payload["jobs"]} == {"A1V1"}


def test_plan_refuses_an_unknown_condition(registry_path, tmp_path, capsys):
    code, _, stderr = run_cli(
        "plan",
        "--registry", registry_path,
        "--out", tmp_path / "outputs",
        "--experiment-id", "EXP-CLI",
        "--conditions", "A9V9",
        capsys=capsys,
    )

    assert code == EXIT_ERROR
    assert "error:" in stderr
    assert "A9V9" in stderr


def test_plan_refuses_an_unknown_case(registry_path, tmp_path, capsys):
    code, _, stderr = run_cli(
        "plan",
        "--registry", registry_path,
        "--out", tmp_path / "outputs",
        "--experiment-id", "EXP-CLI",
        "--cases", "CASE-NOPE",
        capsys=capsys,
    )

    assert code == EXIT_ERROR
    assert "CASE-NOPE" in stderr


def test_plan_refuses_a_missing_registry(tmp_path, capsys):
    code, _, stderr = run_cli(
        "plan",
        "--registry", tmp_path / "absent.json",
        "--out", tmp_path / "outputs",
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )

    assert code == EXIT_ERROR
    assert "error:" in stderr
    # A message an operator can act on, not a traceback.
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------
# run: the transport must be explicit
# --------------------------------------------------------------------------


def test_run_without_a_transport_is_a_usage_error_and_runs_nothing(
    registry_path, tmp_path, capsys
):
    """The single most important safety property in the gate."""
    out = tmp_path / "outputs"
    code, _, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", "whatever.json",
        capsys=capsys,
    )

    assert code == EXIT_USAGE
    assert "needs a transport" in stderr
    assert "--responses" in stderr and "--live" in stderr
    # Nothing was written, and in particular no directory was created that a
    # later command could mistake for an experiment.
    assert not out.exists()


def test_live_and_scripted_together_are_refused(registry_path, tmp_path, capsys):
    code, _, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", tmp_path / "outputs",
        "--plan", "whatever.json",
        "--live",
        "--responses", "whatever.json",
        capsys=capsys,
    )

    assert code == EXIT_USAGE
    assert "different transports" in stderr


def test_live_without_credentials_fails_before_any_paid_call(
    registry_path, tmp_path, capsys, monkeypatch
):
    """§12: fail clearly if the key is missing, and never print it.

    Everything else is valid -- registry, plan, output root -- so the only thing
    that can stop the run is the missing credential. That is what makes this a
    test of the credential check rather than of argument validation.
    """
    monkeypatch.delenv("PILOT01_API_KEY", raising=False)
    monkeypatch.delenv("PILOT01_BASE_URL", raising=False)

    out, paths, _, _ = _scripted_setup(tmp_path, registry_path, capsys)

    code, stdout, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--live",
        capsys=capsys,
    )

    assert code == EXIT_USAGE
    assert "PILOT01_API_KEY" in stderr
    assert "PILOT01_BASE_URL" in stderr
    assert "never printed" in stderr or "ever printed" in stderr
    assert stdout == ""
    # Nothing was executed, so no run directory exists to mistake for data.
    assert not paths.raw_dir.exists() or not any(paths.raw_dir.iterdir())


def test_live_with_a_credential_set_does_not_print_it(
    registry_path, tmp_path, capsys, monkeypatch
):
    """The value must not appear even when it is present and the run proceeds.

    The network is blocked, so the live call fails at the transport. That is the
    interesting case for this assertion: a failure path is where a careless
    implementation includes the request -- and the request carries the
    credential -- in its error message.
    """
    import socket

    # Deliberately not key-shaped: the network guard forbids credential-shaped
    # literals in tracked files, and this test does not need one. Any distinctive
    # string proves the value was not printed.
    secret = "NOT-A-REAL-CREDENTIAL-0123456789"
    monkeypatch.setenv("PILOT01_API_KEY", secret)
    monkeypatch.setenv("PILOT01_BASE_URL", "https://example.invalid/v1")

    def refuse(*args, **kwargs):
        raise OSError("network is blocked in this test")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    out, paths, _, _ = _scripted_setup(tmp_path, registry_path, capsys)

    code, stdout, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--live",
        "--quiet",
        capsys=capsys,
    )

    combined = stdout + stderr
    assert secret not in combined
    # The credential check passed, so the run was attempted rather than refused
    # for a missing key -- which is what makes the absence of the secret
    # meaningful rather than a consequence of never getting that far.
    assert "PILOT01_API_KEY" not in stderr
    assert code in (EXIT_OK, EXIT_ERROR)


def test_the_help_text_says_live_spends_money(capsys):
    """The operator has to be able to find out without reading the source."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--help"])
    help_text = capsys.readouterr().out
    assert "Spends money" in help_text
    assert "deterministic scripted run" in help_text or "--responses" in help_text


# --------------------------------------------------------------------------
# run: scripted mode, end to end
# --------------------------------------------------------------------------


def _scripted_setup(tmp_path, registry_path, capsys, *, conditions="A1V1"):
    """plan -> write responses -> return the paths the next command needs."""
    out = tmp_path / "outputs"
    code, _, stderr = run_cli(
        "plan",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        "--conditions", conditions,
        "--cases", fixture.CASE_A,
        capsys=capsys,
    )
    assert code == EXIT_OK, stderr

    paths = ExperimentPaths(root=out, experiment_id="EXP-CLI")
    from pilot01.experiment import RunPlan, load_response_script

    plan = RunPlan.load_json(paths.run_plan_json)
    script = fixture.script_for_plan(plan, _registry())
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps(script), encoding="utf-8")
    return out, paths, responses, plan


def test_run_executes_a_scripted_plan(registry_path, tmp_path, capsys):
    out, paths, responses, plan = _scripted_setup(tmp_path, registry_path, capsys)

    code, stdout, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    assert "transport: scripted" in stdout
    assert "completed=" in stdout
    assert paths.batch_index_json.exists()
    for job in plan.jobs:
        assert RunDirectory(paths.run_dir(job.run_id)).is_complete()


def test_run_quiet_prints_only_the_counts(registry_path, tmp_path, capsys):
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)

    code, stdout, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        "--quiet",
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    assert "[1/" not in stdout
    assert "runs:" in stdout


def test_run_refuses_to_overwrite_without_resume(registry_path, tmp_path, capsys):
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    argv = [
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
    ]

    assert run_cli(*argv, capsys=capsys)[0] == EXIT_OK
    code, _, stderr = run_cli(*argv, capsys=capsys)

    assert code == EXIT_ERROR
    assert "Refusing to overwrite" in stderr


def test_run_resume_is_a_no_op_on_a_finished_batch(registry_path, tmp_path, capsys):
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    argv = [
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
    ]
    assert run_cli(*argv, capsys=capsys)[0] == EXIT_OK

    code, stdout, stderr = run_cli(*argv, "--resume", capsys=capsys)

    assert code == EXIT_OK, stderr
    assert "skipped=" in stdout


def test_run_fails_the_batch_clearly_when_a_script_is_incomplete(
    registry_path, tmp_path, capsys
):
    out, paths, _, _ = _scripted_setup(tmp_path, registry_path, capsys)
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")

    code, _, stderr = run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", empty,
        capsys=capsys,
    )

    assert code == EXIT_ERROR
    assert "declares no responses for run" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------
# score and summary
# --------------------------------------------------------------------------


def test_score_writes_one_row_per_run(registry_path, tmp_path, capsys):
    out, paths, responses, plan = _scripted_setup(tmp_path, registry_path, capsys)
    run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        capsys=capsys,
    )

    code, stdout, stderr = run_cli(
        "score",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    assert f"scored {len(plan.jobs)} run(s)" in stdout
    lines = [
        line
        for line in paths.run_scores_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == len(plan.jobs)

    with paths.run_scores_csv.open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == len(plan.jobs)


def test_score_needs_no_plan_file(registry_path, tmp_path, capsys):
    """§10: processed results come from raw artifacts, so the plan is not an input."""
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        capsys=capsys,
    )

    paths.run_plan_json.unlink()

    code, stdout, stderr = run_cli(
        "score",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    assert paths.run_scores_jsonl.exists()


def test_summary_reports_counts_and_no_statistics(registry_path, tmp_path, capsys):
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        capsys=capsys,
    )

    code, stdout, stderr = run_cli(
        "summary",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr
    assert "run(s)" in stdout
    assert "accuracy:" in stdout
    assert "correction stages:" in stdout
    assert paths.summary_json.exists()

    payload = json.loads(paths.summary_json.read_text(encoding="utf-8"))
    qc = payload["qc"]
    for field in (
        "planned_runs",
        "attempted_runs",
        "completed_runs",
        "failed_runs",
        "protocol_failure_rate",
        "structured_output_success_rate",
        "verification_failure_count",
        "tool_failure_count",
        "missing_cells",
        "duplicate_cells",
    ):
        assert field in qc, f"§11 requires {field} in the QC summary"

    # The plan was present, so the cell checks were made against it.
    assert qc["planned_runs"] == 2  # one case, one condition, both arms
    assert qc["attempted_runs"] == 2
    assert qc["missing_cells"] == []
    assert qc["duplicate_cells"] == []
    assert qc["plan_fingerprint"].startswith("sha256:")

    # §11: counts only. Nothing inferential.
    lowered = json.dumps(qc).lower()
    for forbidden in ("p_value", "pvalue", "confidence_interval", "bootstrap", "stderr"):
        assert forbidden not in lowered


def test_summary_with_a_pricing_table_prices_the_runs(registry_path, tmp_path, capsys):
    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        capsys=capsys,
    )
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "pricing_version": "cli-test",
                "currency": "USD",
                "input_per_million": {"gpt-4o-mini": 1.0},
                "output_per_million": {"gpt-4o-mini": 2.0},
            }
        ),
        encoding="utf-8",
    )

    code, _, stderr = run_cli(
        "summary",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        "--pricing", pricing,
        capsys=capsys,
    )

    assert code == EXIT_OK, stderr


def test_a_missing_pricing_table_is_an_operator_error(registry_path, tmp_path, capsys):
    code, _, stderr = run_cli(
        "summary",
        "--registry", registry_path,
        "--out", tmp_path / "outputs",
        "--experiment-id", "EXP-CLI",
        "--pricing", tmp_path / "absent.json",
        capsys=capsys,
    )

    assert code == EXIT_ERROR
    assert "error:" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------


def test_the_whole_cli_path_is_offline(registry_path, tmp_path, capsys, monkeypatch):
    """plan -> run -> score -> summary, with the network made fatal.

    A scripted run has no reason to open a socket, and this proves it rather
    than asserting it: any connection attempt raises.
    """
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the CLI opened a socket outside a --live run")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    out, paths, responses, _ = _scripted_setup(tmp_path, registry_path, capsys)
    assert run_cli(
        "run",
        "--registry", registry_path,
        "--out", out,
        "--plan", paths.run_plan_json,
        "--responses", responses,
        "--quiet",
        capsys=capsys,
    )[0] == EXIT_OK
    assert run_cli(
        "score",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )[0] == EXIT_OK
    assert run_cli(
        "summary",
        "--registry", registry_path,
        "--out", out,
        "--experiment-id", "EXP-CLI",
        capsys=capsys,
    )[0] == EXIT_OK

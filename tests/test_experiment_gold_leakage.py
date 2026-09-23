"""§15: gold must not reach anything the model sees.

Two kinds of evidence, because either alone is weak. The architectural tests
read the import graph and prove the *possibility* is absent -- no module on the
model-facing side can name the scoring layer, so it cannot consult gold even by
accident. The behavioural tests execute a real batch and search every byte the
model was sent, which catches a leak that arrived by some route the import graph
does not describe (a registry passed as data, a config field, an environment
variable).

The direction that matters is one-way:

    case registry  ->  scoring layer        (permitted, and the point)
    case registry  ->  agent / prompt / retrieval   (forbidden)
"""

from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path

import pytest

import fixture_registry as fixture
from pilot01.experiment import ExperimentPaths, score_experiment
from pilot01.schemas import Decision

# The canonical definitions of "a treatment token" and "a treatment leak" live in
# the Gate 2.5 isolation suite, which owns that contract. Imported rather than
# restated: two copies of this regex would drift, and the one that drifted would
# be the one that stopped catching leaks.
from test_treatment_isolation import CONDITION_TOKEN_RE, TREATMENT_LEAKS

SRC = Path(__file__).resolve().parent.parent / "src" / "pilot01"

#: Modules on the model-facing side. Nothing here may reach the scoring layer.
MODEL_FACING = (
    "workflow/nodes/manager.py",
    "workflow/nodes/compliance.py",
    "workflow/nodes/analyst.py",
    "workflow/nodes/llm_agent.py",
    "workflow/nodes/tool_loop.py",
    "workflow/nodes/render.py",
    "workflow/nodes/script.py",
    "workflow/runner.py",
    "workflow/state.py",
    "workflow/transitions.py",
    "workflow/views.py",
    "source/tools.py",
    "source/document.py",
    "source/index.py",
    "source/ledger.py",
    "source/verification.py",
    "source/cuad.py",
    "prompts.py",
    "model/client.py",
    "model/scripted.py",
    "model/openai_compat.py",
    "model/parsing.py",
    "model/log.py",
)

#: The scoring layer itself, which is *expected* to import gold-bearing things.
SCORING_MODULES = ("experiment/scoring.py", "experiment/cases.py")


def imported_names(path: Path) -> set[str]:
    """Every module name a file imports, by parsing it rather than importing it.

    Parsing keeps the check honest about *source* dependencies: importing the
    module would execute it, and a module that imported scoring at runtime inside
    a function would still show up here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            # `from . import scoring` names no module, so keep the members too.
            names.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
    return names


def imports_experiment(names: set[str]) -> bool:
    """Whether any import names the experiment package.

    Both spellings count: the absolute ``pilot01.experiment`` and the relative
    ``.experiment`` / ``..experiment`` that a sibling module would write.
    """
    for name in names:
        parts = name.split(".")
        if "experiment" in parts or name.startswith("experiment"):
            return True
    return False


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry():
    return _registry()


@pytest.fixture(scope="module")
def executed(tmp_path_factory):
    """One batch, run once, that every behavioural test below inspects."""
    tmp_path = tmp_path_factory.mktemp("leakage")
    plan = fixture.make_plan(registry=_registry(), condition_ids=("A1V1", "A1V0", "A0V0"))
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    batch = fixture.execute(plan, _registry(), paths)
    return paths, batch, plan


# --------------------------------------------------------------------------
# The import graph
# --------------------------------------------------------------------------


@pytest.mark.parametrize("relative", MODEL_FACING)
def test_no_model_facing_module_can_reach_the_scoring_layer(relative):
    path = SRC / relative
    assert path.exists(), f"{relative} is listed but missing; update MODEL_FACING"

    names = imported_names(path)

    assert not imports_experiment(names), (
        f"{relative} imports the experiment package. Scoring may read the case "
        f"registry; nothing the model can reach may. Offending imports: "
        f"{sorted(n for n in names if 'experiment' in n)}"
    )


@pytest.mark.parametrize("relative", SCORING_MODULES)
def test_the_scoring_layer_is_where_gold_is_allowed(relative):
    """The other half of the assertion: the ban is on the direction, not the data."""
    path = SRC / relative
    assert path.exists()

    source = path.read_text(encoding="utf-8")

    assert "gold" in source.lower(), (
        f"{relative} is listed as a scoring module but never mentions gold; "
        "if it has stopped handling gold, it does not belong in this list"
    )


def test_the_gold_ban_is_not_vacuous():
    """A guard that cannot fail is not a guard.

    ``imports_experiment`` must actually recognise the imports it is looking for,
    or the parametrized tests above would pass on a codebase that leaked.
    """
    assert imports_experiment({"pilot01.experiment"})
    assert imports_experiment({"pilot01.experiment.scoring"})
    assert imports_experiment({".experiment"})
    assert imports_experiment({"..experiment.scoring"})
    assert imports_experiment({"experiment"})
    assert not imports_experiment({"pilot01.source", "pilot01.schemas", "json"})
    # A name that merely contains the word is not the package.
    assert not imports_experiment({"pilot01.experimentation_helpers"})


def test_a_model_facing_module_that_did_import_scoring_would_be_caught(tmp_path):
    """The detector runs against a real file, not just a hand-made set."""
    planted = tmp_path / "leaky.py"
    planted.write_text(
        "from pilot01.experiment.scoring import score_run\n", encoding="utf-8"
    )
    assert imports_experiment(imported_names(planted))

    clean = tmp_path / "clean.py"
    clean.write_text("from pilot01.schemas import Decision\n", encoding="utf-8")
    assert not imports_experiment(imported_names(clean))


# --------------------------------------------------------------------------
# What the model was actually sent
# --------------------------------------------------------------------------


GOLD_KEY_PATTERNS = (
    "gold",
    "hidden",
    "omission",
    "omit",
    "scoring",
    "sentinel",
    "is_negative",
)


def walk_keys(value, prefix=""):
    """Every key path in a nested JSON-shaped value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{prefix}.{key}" if prefix else str(key)
            yield from walk_keys(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_keys(item, f"{prefix}[{index}]")


def test_no_raw_model_call_mentions_gold_anywhere(executed):
    """The whole request, not just the fields the renderer is supposed to use."""
    paths, _, _ = executed
    checked = 0

    for directory in sorted(paths.raw_dir.iterdir()):
        log = directory / "model_calls.jsonl"
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            checked += 1
            rendered = record["rendered_input"]
            lowered = rendered.lower()
            for pattern in GOLD_KEY_PATTERNS:
                assert pattern not in lowered, (
                    f"{record['role']} was sent a request containing {pattern!r}. "
                    "Gold belongs to the scoring layer and must not reach a prompt."
                )

    assert checked > 0, "no model calls were inspected; the test proved nothing"


def test_no_raw_model_call_carries_a_gold_key_in_its_structured_fields(executed):
    """``parsed_output`` and ``tool_request`` are the model's own JSON, unquoted.

    A key could arrive here that never appeared in the rendered text if the model
    echoed something it was given out of band.
    """
    paths, _, _ = executed
    checked = 0

    for directory in sorted(paths.raw_dir.iterdir()):
        log = directory / "model_calls.jsonl"
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for field in ("parsed_output", "tool_request"):
                payload = record.get(field)
                if payload is None:
                    continue
                checked += 1
                for key_path in walk_keys(payload):
                    leaf = key_path.rsplit(".", 1)[-1].lower()
                    for pattern in GOLD_KEY_PATTERNS:
                        assert pattern not in leaf, (
                            f"{record['role']}'s {field} carries the key "
                            f"{key_path!r}, which is gold-shaped"
                        )

    assert checked > 0, "no structured model output was inspected"


def test_no_gold_reaches_a_tool_argument(executed):
    """The tools are the other way out of the sandbox, so they get the same check."""
    paths, _, _ = executed
    checked = 0

    for directory in sorted(paths.raw_dir.iterdir()):
        log = directory / "tool_calls.jsonl"
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            checked += 1
            blob = json.dumps(record).lower()
            for pattern in ("gold", "hidden", "omission", "sentinel"):
                assert pattern not in blob, (
                    f"a {record.get('tool')} call carries {pattern!r}: {blob[:400]}"
                )

    assert checked > 0, "no tool calls were inspected"


def test_the_treatment_labels_never_reach_a_prompt(executed):
    """§15: the model must not be able to read its own experimental arm.

    ``E0``/``A1V1`` are the experiment's words. A model that saw them could
    reason about the design instead of the contract, and the pilot's whole claim
    is that the agents did not know which arm they were in.

    Checked over the raw call log rather than over a rendered prompt in
    isolation, so a leak introduced anywhere in the request-building path is
    caught. The case id is deliberately not in the forbidden set: a case's name
    is part of the material, and ``CASE: CASE-A`` is the renderer naming its
    input, not the experiment naming its arm. The run id is checked separately by
    the batch suite.
    """
    paths, _, _ = executed

    inspected = 0
    for directory in sorted(paths.raw_dir.iterdir()):
        log = directory / "model_calls.jsonl"
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            inspected += 1
            rendered = record["rendered_input"]
            found = CONDITION_TOKEN_RE.findall(rendered)
            assert not found, (
                f"{record['role']} was sent the treatment token(s) {found}"
            )
            for leak in TREATMENT_LEAKS:
                assert leak not in rendered.lower(), (
                    f"{record['role']} was sent {leak!r}, which states the design"
                )

    assert inspected > 0, "no model calls were inspected; the test proved nothing"


def test_gold_offsets_are_an_observation_the_agent_earned_not_an_input(executed):
    """§15: the target offsets enter only after the run.

    The fixture's gold clause *is* the paragraph the agent opens, so its offsets
    legitimately reach the model -- as the body of the tool result, in the turn
    after the agent asked for that paragraph. Asserting the numbers are absent
    would be false.

    The claim that must hold is the ordering, so that is what is checked: no
    request made *before* the agent opens a paragraph contains its offsets. An
    agent handed the target location up front would have nothing left to
    retrieve, and the retrieval metrics would measure nothing.
    """
    paths, _, _ = executed

    target_spans = _registry().get(fixture.CASE_A).gold_spans()
    assert target_spans, "the fixture case must have a gold span for this test"
    start, _ = target_spans[0]
    # The tool result renders offsets as a prose range, not as JSON, so the
    # needle has to match what the renderer actually writes.
    needle = f"offsets: {start}.."

    later_turns = 0
    for directory in sorted(paths.raw_dir.iterdir()):
        model_log = directory / "model_calls.jsonl"
        tool_log = directory / "tool_calls.jsonl"
        if not model_log.exists():
            continue

        records = [
            json.loads(line)
            for line in model_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for record in records:
            if needle not in record["rendered_input"]:
                continue
            # The offset is present. A role's very first turn is made before any
            # tool result can exist, so it is the turn that would carry a
            # pre-supplied answer if there were one.
            assert record["call_index"] > 0, (
                f"{record['role']}'s first turn already contains the gold offset; "
                "the target location was handed to the agent instead of retrieved"
            )
            later_turns += 1

        # And the offsets are never a tool *argument*: an agent cannot ask for a
        # paragraph by its coordinates, only by its id.
        if tool_log.exists():
            assert needle not in tool_log.read_text(encoding="utf-8"), (
                "the gold offset was passed as a tool argument"
            )

    assert later_turns > 0, (
        "the fixture opens the gold paragraph, so the offset should reach the model "
        "in the following turn -- if it does not, this test is checking nothing"
    )

    # The same numbers reach the scored output, which is where the registry's
    # copy of them belongs.
    scores = score_experiment(paths.raw_dir, registry=_registry())
    scored = {span for row in scores.rows for span in row.manager_evidence.gold_spans}
    assert target_spans[0] in scored


# --------------------------------------------------------------------------
# The registry itself stays out of the sandbox
# --------------------------------------------------------------------------


def test_a_raw_artifact_refuses_a_gold_shaped_key(executed):
    """The last line: even a leak that got as far as an artifact is rejected."""
    paths, _, plan = executed
    run_json = paths.run_dir(plan.jobs[0].run_id) / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))

    payload["gold_action"] = "ESCALATE"

    from pilot01.experiment import RunArtifact

    with pytest.raises(Exception, match="gold"):
        RunArtifact.from_payload(payload)


def test_the_execution_record_has_no_gold_field_at_all(executed):
    """Not merely absent from this run -- not in the schema."""
    paths, _, plan = executed
    run_json = paths.run_dir(plan.jobs[0].run_id) / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))

    for key_path in walk_keys(payload):
        leaf = key_path.rsplit(".", 1)[-1].lower()
        for pattern in GOLD_KEY_PATTERNS:
            assert pattern not in leaf, f"run.json carries the gold-shaped key {key_path!r}"


def test_the_case_registry_holds_the_gold_the_artifacts_do_not(executed):
    """The contrast that makes the previous tests meaningful.

    Note what this test does *not* claim. The string ``ESCALATE`` does appear in
    ``run.json`` -- because an agent decided it. That is an answer, and answers
    are the data. What is absent is the *label*: nothing in the artifact records
    which answer was correct, and no field exists to hold it. The distinction is
    the whole of §15, so it is worth stating rather than testing something
    weaker that happens to pass.
    """
    paths, _, _ = executed

    case = _registry().get(fixture.CASE_A)
    assert case.gold_action is Decision.ESCALATE
    assert case.gold_spans()

    run_json = json.loads(
        (paths.run_dir(sorted(p.name for p in paths.raw_dir.iterdir())[0]) / "run.json").read_text(
            encoding="utf-8"
        )
    )
    # The agent's answer is there, as data.
    assert run_json["execution"]["compliance_output"]["decision"] == "ESCALATE"
    # The label that would say whether it was right is not, and cannot be: every
    # key in the artifact is accounted for by the public schema.
    keys = set(walk_keys(run_json))
    assert not any("gold" in key.lower() for key in keys)
    assert not any("correct" in key.lower() for key in keys)
    assert not any("sentinel" in key.lower() for key in keys)

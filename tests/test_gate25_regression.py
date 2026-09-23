"""Gate 2.5, group 7: the earlier gates still hold.

Gate 2.5 added a tool layer, a retrieval index and a verification gate to a
workflow that already worked. The risk in that kind of change is not that the
new thing is wrong -- the other six groups test that -- it is that the old thing
quietly stopped being true. So this file re-asserts the properties Gate 1, 1.5
and 2 established, at the level of the property rather than the implementation,
and one test re-runs the earlier suite as it stands.

What is checked here:

* **The earlier suite still passes**, run as its own pytest invocation so a
  failure in it is reported as a regression rather than as a Gate 2.5 failure.
* **The workflow is still the fixed sequential one.** Source, Manager,
  Compliance, final: no branching, no graph, no second orchestrator.
* **E0 and E1 still differ only in the memo.** The error condition is an input,
  never a code path, and nothing on the Python side ever rewrites an answer
  toward the expected one.
* **Isolation still holds with the source layer switched on.** The new tools are
  the one new way data can reach an agent, so the isolation audit is re-run with
  them live: no memo to Compliance, no gold anywhere, no treatment label in any
  request.
* **The default suite is still offline.** No test in this repository opens a
  socket.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import pilot01
import sample_case
from pilot01.config import load_conditions_v1, load_models_v1, load_workflow_v1
from pilot01.model import ModelCallLog, ScriptedModelClient
from pilot01.schemas import ClauseStatus, Decision, ErrorCondition, VerificationStatus
from pilot01.source import SourceLibrary, ToolCallLog, VerificationLog
from pilot01.workflow.nodes import FrozenMemoRepository, build_llm_registry
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState, RunStatus
from pilot01.workflow.transitions import ProtocolError, TransitionKind
from pilot01.workflow.views import build_compliance_view, build_manager_view

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

GATE_25_TEST_FILES = frozenset(
    {
        "test_source_retrieval.py",
        "test_source_access_conditions.py",
        "test_treatment_isolation.py",
        "test_evidence_provenance.py",
        "test_tool_loop.py",
        "test_gate25_regression.py",
    }
)
"""Files added by Gate 2.5 -- excluded from the "earlier suite" re-run."""

LATER_GATE_TEST_FILES = frozenset(
    {
        "test_experiment_artifacts.py",
        "test_experiment_batch.py",
        "test_experiment_cli.py",
        "test_experiment_gold_leakage.py",
        "test_experiment_plan.py",
        "test_experiment_scenarios.py",
        "test_experiment_scoring.py",
        "test_gate3_regression.py",
    }
)
"""Files added by a later gate, excluded for the same reason and one more.

The re-run below means "the suite as it stood before Gate 2.5". A module written
afterwards is not that, however it behaves. The extra reason is structural:
``test_gate3_regression.py`` re-runs *its* predecessors in a subprocess of its
own, so leaving it in this set would nest the two re-runs inside each other
without terminating.
"""

EARLIER_TEST_FILES: tuple[str, ...] = tuple(
    sorted(
        path.name
        for path in TESTS_ROOT.glob("test_*.py")
        if path.name not in GATE_25_TEST_FILES | LATER_GATE_TEST_FILES
    )
)
"""Every test module that existed before Gate 2.5, discovered rather than listed.

Discovered so a module added later cannot be silently left out of the re-run;
filtered by explicit exclusion sets so this file cannot include itself and
recurse.
"""

TARGET_PARAGRAPH = sample_case.paragraph_id_containing(sample_case.CHANGE_OF_CONTROL_PHRASE)

CONDITION_TOKEN_RE = __import__("re").compile(
    r"\b(?:A0V0|A1V0|A1V1|A0V1|A0|A1|V0|V1|E0|E1)\b"
)
HIDDEN_MARKERS: tuple[str, ...] = (
    "gold",
    "hidden",
    "omission",
    "omitted",
    "hypothesis",
    "supposed to fail",
    "error_condition",
    "treatment",
    "condition_id",
)
"""Strings that must never reach a model, matched case-insensitively."""


def search(query: str = "change of control") -> str:
    return sample_case.tool_request_text("search_contract", query=query)


def open_span(paragraph_id: str = TARGET_PARAGRAPH) -> str:
    return sample_case.tool_request_text("open_source_span", paragraph_id=paragraph_id)


# --------------------------------------------------------------------------
# The earlier suite still passes
# --------------------------------------------------------------------------


def test_the_earlier_suite_is_discovered_and_non_trivial():
    """Guard the guard: a re-run of nothing would pass trivially."""
    assert EARLIER_TEST_FILES
    assert "test_isolation.py" in EARLIER_TEST_FILES
    assert "test_llm_nodes.py" in EARLIER_TEST_FILES
    assert not (set(EARLIER_TEST_FILES) & GATE_25_TEST_FILES)


def test_every_earlier_gate_test_still_passes():
    """Gate 1 / 1.5 / 2, re-run as its own pytest invocation.

    A separate process on purpose: a failure inside the earlier suite is a
    regression in work that was already accepted, and running it here means it
    is reported as one -- with the earlier suite's own output -- rather than
    being folded into a Gate 2.5 failure.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *[str(TESTS_ROOT / name) for name in EARLIER_TEST_FILES],
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the pre-Gate-2.5 suite no longer passes:\n{result.stdout[-4000:]}"
    )
    assert " passed" in result.stdout, result.stdout[-2000:]
    # ...and it really ran the earlier suite, rather than collecting nothing.
    reported = int(result.stdout.rsplit(" passed", 1)[0].split()[-1])
    assert reported > 300, reported


# --------------------------------------------------------------------------
# The workflow is still the fixed sequential one
# --------------------------------------------------------------------------


def test_the_workflow_is_unchanged():
    """Analyst -> Manager -> Compliance: the fixed sequence, three nodes."""
    workflow = load_workflow_v1()
    assert tuple(workflow.nodes) == ("analyst", "manager", "compliance")
    assert workflow.start == "analyst"
    # Every edge is unconditional: one `default` per node, and no revision edge.
    for node, transitions in workflow.edges.items():
        assert set(transitions) == {TransitionKind.DEFAULT}, (node, transitions)
        assert TransitionKind.REQUEST_REVISION not in transitions
    assert workflow.max_revision_rounds == 0
    assert workflow.max_steps >= len(workflow.nodes)


def test_the_main_conditions_are_unchanged():
    conditions = load_conditions_v1()
    assert conditions.main_conditions == ("A0V0", "A1V0", "A1V1")
    assert set(conditions.excluded_conditions) == {"A0V1"}
    for condition_id in conditions.main_conditions:
        assert condition_id in conditions.conditions
    # A0V1 stays excluded from the main design.
    assert "A0V1" not in conditions.main_conditions


def test_no_second_orchestration_framework_was_introduced():
    """No LangGraph, no graph library, no scheduler: one runner and one loop."""
    forbidden = ("langgraph", "langchain", "networkx", "prefect", "airflow", "celery")
    for path in Path(pilot01.__file__).resolve().parent.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for name in forbidden:
            assert f"import {name}" not in text, f"{path.name} imports {name}"


def test_the_runner_still_owns_control_flow_between_nodes():
    """The tool loop is inside a node; the node sequence is still the runner's."""
    from pilot01.workflow import runner as runner_module

    text = Path(runner_module.__file__).read_text(encoding="utf-8")
    assert "run_tool_loop" not in text


# --------------------------------------------------------------------------
# E0 / E1 differ only in the memo
# --------------------------------------------------------------------------


class Identical:
    """A run whose two error conditions get byte-identical scripted answers."""

    def __init__(self, error_condition: ErrorCondition) -> None:
        self.error_condition = error_condition
        self.repository = FrozenMemoRepository.from_e0_memo(
            sample_case.build_e0_memo(),
            target_claim_id=sample_case.TARGET_CLAIM_ID,
            expected_category=sample_case.TARGET_CATEGORY,
        )
        self.conditions = load_conditions_v1()
        self.workflow = load_workflow_v1()
        self.call_log = ModelCallLog()
        self.tool_log = ToolCallLog()
        self.manager_client = ScriptedModelClient((sample_case.manager_response_text(),))
        self.compliance_client = ScriptedModelClient(
            (sample_case.compliance_response_text(),)
        )
        self.registry = build_llm_registry(
            repository=self.repository[0],
            models=load_models_v1(),
            manager_client=self.manager_client,
            compliance_client=self.compliance_client,
            call_log=self.call_log,
            source=sample_case.build_source_library(),
            tool_log=self.tool_log,
            verification_log=VerificationLog(),
        )
        self.state = ExperimentState.create(
            run_id="run-0001",
            case_id=sample_case.CASE_ID,
            repetition_id=0,
            condition_id="A1V0",
            conditions=self.conditions,
            error_condition=error_condition,
            contract_id=sample_case.CONTRACT_ID,
            contract_text_hash=sample_case.CONTRACT_TEXT_HASH,
            target_category=sample_case.TARGET_CATEGORY,
            memo=self.repository[0].load(sample_case.CASE_ID, error_condition),
            gold_target_clause_status={sample_case.TARGET_CATEGORY: ClauseStatus.PRESENT, sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.ABSENT},
            policy=sample_case.build_policy(),
            omission=(
                None if error_condition is ErrorCondition.E0 else self.repository[1]
            ),
        )
        self.error: ProtocolError | None = None

    def go(self):
        runner = WorkflowRunner(
            workflow=self.workflow, conditions=self.conditions, registry=self.registry
        )
        self.runner = runner
        try:
            runner.run(self.state)
        except ProtocolError as exc:
            self.error = exc
        return self.state


def test_the_error_condition_changes_only_the_memo():
    """Same script in, same script out. E0/E1 is an input, not a code path."""
    e0, e1 = Identical(ErrorCondition.E0), Identical(ErrorCondition.E1)
    e0.go()
    e1.go()
    assert e0.error is None and e1.error is None
    # The answers the model gave are the same, because the model was scripted
    # identically. Any difference downstream would be Python rewriting an answer.
    assert e0.state.manager_output == e1.state.manager_output
    assert e0.state.compliance_output == e1.state.compliance_output
    assert e0.state.status is e1.state.status is RunStatus.COMPLETED


def test_the_error_condition_changes_the_memo_the_agent_receives():
    """...and the memo really is different, so the test above is not vacuous."""
    e0, e1 = Identical(ErrorCondition.E0), Identical(ErrorCondition.E1)
    left = build_manager_view(e0.state).analyst_memo
    right = build_manager_view(e1.state).analyst_memo
    assert left != right
    assert len(left.claims) != len(right.claims)


def test_no_python_code_rewrites_an_answer_toward_the_gold_one():
    """A model answer that disagrees with gold is carried through unchanged."""
    wrong = sample_case.manager_response_text(
        clause_status=ClauseStatus.ABSENT,
        decision=Decision.ACCEPT,
        confidence=0.1,
    )
    run = Identical(ErrorCondition.E0)
    run.manager_client = ScriptedModelClient((wrong,))
    run.registry = build_llm_registry(
        repository=run.repository[0],
        models=load_models_v1(),
        manager_client=run.manager_client,
        compliance_client=ScriptedModelClient((sample_case.compliance_response_text(),)),
        call_log=ModelCallLog(),
        source=sample_case.build_source_library(),
        tool_log=ToolCallLog(),
        verification_log=VerificationLog(),
    )
    run.go()
    assert run.error is None
    assert run.state.manager_output is not None
    assert (
        run.state.manager_output.target_clause_status[sample_case.TARGET_CATEGORY]
        is ClauseStatus.ABSENT
    )
    assert run.state.manager_output.decision is Decision.ACCEPT
    # Gold says present/escalate. The run kept the model's answer anyway.
    assert run.state.hidden.gold.status_for(sample_case.TARGET_CATEGORY) is ClauseStatus.PRESENT


def test_the_two_error_conditions_produce_the_same_node_sequence():
    """Same nodes, same order. The error condition selects an input, not a path."""
    sequences = []
    for error_condition in (ErrorCondition.E0, ErrorCondition.E1):
        run = Identical(error_condition)
        run.go()
        sequences.append(
            tuple(
                event.node
                for event in run.runner._collector.events
                if getattr(event, "node", None) is not None
            )
        )
    assert sequences[0]
    assert sequences[0] == sequences[1]


# --------------------------------------------------------------------------
# Isolation, re-audited with the source layer live
# --------------------------------------------------------------------------


def live_requests(*, condition_id: str = "A1V1") -> tuple:
    """Every request both agents made in a run with the tools switched on."""
    repository = FrozenMemoRepository.from_e0_memo(
        sample_case.build_e0_memo(),
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )[0]
    conditions = load_conditions_v1()
    manager_client = ScriptedModelClient(
        (search(), search("assignment"), open_span(), sample_case.manager_response_text(
            evidence_ids=(TARGET_PARAGRAPH,),
            verification_status=VerificationStatus.VERIFIED,
        ))
    )
    compliance_client = ScriptedModelClient(
        (search(), search("assignment"), open_span(), sample_case.compliance_response_text(
            evidence_ids=(TARGET_PARAGRAPH,),
            verification_status=VerificationStatus.VERIFIED,
        ))
    )
    registry = build_llm_registry(
        repository=repository,
        models=load_models_v1(),
        manager_client=manager_client,
        compliance_client=compliance_client,
        call_log=ModelCallLog(),
        source=sample_case.build_source_library(),
        tool_log=ToolCallLog(),
        verification_log=VerificationLog(),
    )
    state = ExperimentState.create(
        run_id="run-0001",
        case_id=sample_case.CASE_ID,
        repetition_id=0,
        condition_id=condition_id,
        conditions=conditions,
        error_condition=ErrorCondition.E0,
        contract_id=sample_case.CONTRACT_ID,
        contract_text_hash=sample_case.CONTRACT_TEXT_HASH,
        target_category=sample_case.TARGET_CATEGORY,
        memo=repository.load(sample_case.CASE_ID, ErrorCondition.E0),
        gold_target_clause_status={sample_case.TARGET_CATEGORY: ClauseStatus.PRESENT, sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.ABSENT},
        policy=sample_case.build_policy(),
        gold_evidence_offsets=(987654321,),
    )
    WorkflowRunner(
        workflow=load_workflow_v1(), conditions=conditions, registry=registry
    ).run(state)
    return manager_client.requests + compliance_client.requests


@pytest.fixture(scope="module")
def live_request_texts() -> tuple[str, ...]:
    return tuple(request.rendered_input() for request in live_requests())


def test_no_request_carries_a_treatment_label(live_request_texts):
    for text in live_request_texts:
        assert not CONDITION_TOKEN_RE.search(text), CONDITION_TOKEN_RE.findall(text)


def test_no_request_carries_hidden_experimental_data(live_request_texts):
    for text in live_request_texts:
        lowered = text.lower()
        for marker in HIDDEN_MARKERS:
            assert marker not in lowered, f"a request mentions {marker!r}"


def test_the_gold_offsets_never_reach_a_request(live_request_texts):
    for text in live_request_texts:
        assert "987654321" not in text
        assert "987654322" not in text


def test_compliance_still_does_not_receive_the_analyst_memo():
    """The new source tools are a second route to the agent; the memo is not.

    The claim *ids* do reach Compliance, because the Manager's handoff names the
    claims it adopted -- that is the handoff doing its job. What must not reach
    it is the memo itself: its identity, its summaries, its count.
    """
    memo = sample_case.build_e0_memo()
    requests = live_requests()
    compliance = [request for request in requests if request.role == "compliance"]
    assert compliance
    for request in compliance:
        text = request.rendered_input()
        assert "ANALYST MEMO" not in text
        assert "memo_id" not in text
        assert memo.memo_id not in text
        assert f"claims: {len(memo.claims)}" not in text
        for claim in memo.claims:
            assert claim.summary not in text
            # The memo's own rendering, which the handoff does not use: a
            # `CLAIM <id>` heading and an indented per-claim status. (The
            # Manager's *own* assessment legitimately reaches Compliance as
            # `clause_status:` in the handoff -- that is the handoff working,
            # not a leak of the analyst's reading.)
            assert f"CLAIM {claim.claim_id}" not in text
            assert f"  status: {claim.status.value}" not in text
            # The id may appear only inside the handoff's claim list.
            if claim.claim_id in text:
                assert "upstream_claim_ids:" in text


def test_the_source_tools_give_compliance_no_route_to_the_memo():
    """The tool payloads are contract text only -- nothing about the analyst."""
    repository = FrozenMemoRepository.from_e0_memo(
        sample_case.build_e0_memo(),
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )[0]
    tools = __import__(
        "pilot01.source.tools", fromlist=["SourceTools"]
    ).SourceTools(node="compliance", library=sample_case.build_source_library())
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    rendered = "\n".join(
        hit.model_dump_json() for hit in tools.search_contract("change of control")
    ) + tools.open_source_span(TARGET_PARAGRAPH).model_dump_json()
    lowered = rendered.lower()
    assert "memo" not in lowered
    assert "analyst" not in lowered
    assert "claim" not in lowered
    for claim in sample_case.build_e0_memo().claims:
        assert claim.claim_id not in rendered
        assert claim.summary not in rendered


def test_the_manager_still_does_not_receive_the_policy_answer():
    """The policy states both branches; neither is marked as the expected one."""
    for text in (request.rendered_input() for request in live_requests()):
        lowered = text.lower()
        assert "you should escalate" not in lowered
        assert "you should accept" not in lowered
        assert "the answer is" not in lowered


# --------------------------------------------------------------------------
# The default suite is still offline
# --------------------------------------------------------------------------


AUDITORS = frozenset({Path(__file__).name, "test_network_guard.py"})
"""Modules whose job is to *name* the thing they audit, so scanning them is circular."""


def test_no_test_module_makes_a_network_call():
    """The new modules are held to the same rule as the old ones."""
    offenders = []
    for path in sorted(TESTS_ROOT.glob("test_*.py")):
        if path.name in AUDITORS:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("requests.get", "requests.post", "urlopen", "socket.socket"):
            if marker in text:
                offenders.append((path.name, marker))
    assert not offenders, offenders


def test_the_live_smoke_test_is_never_collected_by_default():
    """Whatever form the optional smoke test takes, pytest must not run it.

    Gate 2.5 must not depend on a paid call, so the smoke test is a script under
    ``scripts/`` rather than a test module, and this asserts that no module in
    ``tests/`` has grown one back.
    """
    for path in TESTS_ROOT.glob("test_*.py"):
        if path.name in AUDITORS:
            continue
        text = path.read_text(encoding="utf-8")
        assert "PILOT01_LIVE_SMOKE" not in text, path.name
    assert not (TESTS_ROOT / "test_live_smoke.py").exists()
    assert not (TESTS_ROOT / "test_smoke.py").exists()

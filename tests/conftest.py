"""Shared fixtures.

All fixtures are deterministic: a frozen clock, a fixed synthetic case, and a
repository whose E1 arm is derived from its E0 arm.

The agent doubles are fixture-controlled, so the default scripts below are part
of the *fixture*, not of ``src/``. They declare a Manager and a Compliance that
report the target clause present and escalate -- a plain, arm-independent
scenario, so that pipeline tests observe the plumbing rather than a behaviour
that production code would otherwise have had to invent.

Gate 2 adds a second, equally explicit wiring: the model-backed registry, driven
by a :class:`~pilot01.model.scripted.ScriptedModelClient`. No fixture here
constructs a live client, and :func:`_block_network` makes an accidental
connection a test failure rather than a bill.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

import sample_case
from pilot01.config import (
    ConditionsConfig,
    ModelsConfig,
    WorkflowConfig,
    load_conditions_v1,
    load_models_v1,
    load_workflow_v1,
)
from pilot01.model import ModelCallLog, ScriptedModelClient
from pilot01.schemas import AnalystMemo, ErrorCondition, OmissionRecord, ClauseStatus
from pilot01.source import SourceLibrary, ToolCallLog, VerificationLog
from pilot01.source.tools import replay_ledger
from pilot01.workflow.nodes import (
    FakeAgentScript,
    FrozenMemoRepository,
    build_llm_registry,
    build_registry,
)
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState
from pilot01.workflow.transitions import NodeRegistry

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

GOLD_OFFSET_SENTINELS = (987654321, 987654322)
"""Deliberately distinctive values, so tests can scan for them in serialized views."""


def fixed_clock() -> datetime:
    return FIXED_TIME


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Make any outbound connection attempted by a test a loud failure.

    The suite must be runnable offline and must never place a paid call. Every
    model-backed test drives a
    :class:`~pilot01.model.scripted.ScriptedModelClient`, so a connection
    attempt means something reached past the boundary -- which is exactly the
    mistake this guard exists to catch. ``socket`` construction is left alone so
    that unrelated machinery is unaffected; only connecting is refused.
    """

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test attempted a network connection. The suite must run offline; "
            "inject a ScriptedModelClient (or another fake transport) instead."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="session")
def conditions() -> ConditionsConfig:
    return load_conditions_v1()


@pytest.fixture(scope="session")
def workflow() -> WorkflowConfig:
    return load_workflow_v1()


@pytest.fixture(scope="session")
def models() -> ModelsConfig:
    """Per-role model parameters. Parameters only -- never a credential."""
    return load_models_v1()


@pytest.fixture(scope="session")
def policy():
    return sample_case.build_policy()


@pytest.fixture(scope="session")
def e0_memo() -> AnalystMemo:
    return sample_case.build_e0_memo()


@pytest.fixture(scope="session")
def pairing(e0_memo) -> tuple[FrozenMemoRepository, OmissionRecord]:
    """The frozen-artifact repository plus the hidden omission provenance."""
    return FrozenMemoRepository.from_e0_memo(
        e0_memo,
        target_claim_id=sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )


@pytest.fixture(scope="session")
def repository(pairing) -> FrozenMemoRepository:
    return pairing[0]


@pytest.fixture(scope="session")
def omission_record(pairing) -> OmissionRecord:
    return pairing[1]


@pytest.fixture
def manager_script() -> FakeAgentScript:
    """Default fixture scenario: the Manager reports the target clause present."""
    return FakeAgentScript(outputs=(sample_case.manager_output(),))


@pytest.fixture
def compliance_script() -> FakeAgentScript:
    """Default fixture scenario: Compliance confirms and escalates."""
    return FakeAgentScript(outputs=(sample_case.compliance_output(),))


@pytest.fixture
def registry(repository, manager_script, compliance_script) -> NodeRegistry:
    return build_registry(
        repository=repository,
        manager_script=manager_script,
        compliance_script=compliance_script,
    )


@pytest.fixture
def make_registry(repository, manager_script, compliance_script):
    """Build a registry, overriding either fake agent's declared script."""

    def _make(
        *,
        manager: FakeAgentScript | None = None,
        compliance: FakeAgentScript | None = None,
    ) -> NodeRegistry:
        return build_registry(
            repository=repository,
            manager_script=manager if manager is not None else manager_script,
            compliance_script=(
                compliance if compliance is not None else compliance_script
            ),
        )

    return _make


@pytest.fixture
def make_state(conditions, repository, omission_record, policy):
    """Build an :class:`ExperimentState` for any (condition, error condition) cell."""

    def _make(
        condition_id: str = "A0V0",
        error_condition: ErrorCondition = ErrorCondition.E0,
        *,
        run_id: str = "run-0001",
        repetition_id: int = 0,
        gold_status: ClauseStatus = ClauseStatus.PRESENT,
    ) -> ExperimentState:
        return ExperimentState.create(
            run_id=run_id,
            case_id=sample_case.CASE_ID,
            repetition_id=repetition_id,
            condition_id=condition_id,
            conditions=conditions,
            error_condition=error_condition,
            contract_id=sample_case.CONTRACT_ID,
            contract_text_hash=sample_case.CONTRACT_TEXT_HASH,
            target_category=sample_case.TARGET_CATEGORY,
            memo=repository.load(sample_case.CASE_ID, error_condition),
            gold_status=gold_status,
            policy=policy,
            gold_evidence_offsets=GOLD_OFFSET_SENTINELS,
            omission=None if error_condition is ErrorCondition.E0 else omission_record,
        )

    return _make


@pytest.fixture
def runner(workflow, conditions, registry) -> WorkflowRunner:
    return WorkflowRunner(
        workflow=workflow,
        conditions=conditions,
        registry=registry,
        clock=fixed_clock,
    )


# --------------------------------------------------------------------------
# Gate 2: the model-backed wiring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCase:
    """A model-backed registry plus the transports and logs it was built with."""

    registry: NodeRegistry
    manager_client: ScriptedModelClient
    compliance_client: ScriptedModelClient
    log: ModelCallLog
    source: SourceLibrary
    tool_log: ToolCallLog
    verification_log: VerificationLog

    def runner(self, workflow: WorkflowConfig, conditions: ConditionsConfig) -> WorkflowRunner:
        return WorkflowRunner(
            workflow=workflow,
            conditions=conditions,
            registry=self.registry,
            clock=fixed_clock,
        )

    def ledger(self, node: str, *, upstream=()):
        """One node's evidence ledger, replayed from the tool log.

        Replayed rather than read off a live object on purpose: this is the same
        path a post-hoc audit takes, so a test that passes here is evidence that
        the run's conclusion is recoverable from the record alone. ``upstream``
        is the evidence the node was handed, which no tool call produced.
        """
        return replay_ledger(self.tool_log.records, node=node, upstream=upstream)

    def calls(self, node: str):
        """The model-call records this run attributed to one node."""
        return tuple(record for record in self.log.records if record.role == node)


@pytest.fixture
def call_log() -> ModelCallLog:
    """A fresh in-memory raw-call log. One per test."""
    return ModelCallLog()


@pytest.fixture
def source_library() -> SourceLibrary:
    """The fixture contract, paragraphized by the source layer itself."""
    return sample_case.build_source_library()


@pytest.fixture
def tool_log() -> ToolCallLog:
    """A fresh in-memory tool-call log. One per test."""
    return ToolCallLog()


@pytest.fixture
def verification_log() -> VerificationLog:
    """A fresh in-memory verification log. One per test."""
    return VerificationLog()


@pytest.fixture
def make_llm_case(repository, models, call_log, source_library, tool_log, verification_log):
    """Build the model-backed registry around scripted transports.

    Both response scripts are required, for the same reason the fake agent
    scripts are: a default response would be a behavioural assumption.

    The source library defaults to the fixture contract; pass ``source=`` to give
    a case a contract of its own, which is how the cross-contract tests arrange
    for one run's ids to be foreign to another's.
    """

    def _make(
        *,
        manager_responses: tuple[str, ...],
        compliance_responses: tuple[str, ...],
        repeat_last: bool = False,
        source: SourceLibrary | None = None,
    ) -> LLMCase:
        library = source if source is not None else source_library
        manager_client = ScriptedModelClient(
            manager_responses, repeat_last=repeat_last
        )
        compliance_client = ScriptedModelClient(
            compliance_responses, repeat_last=repeat_last
        )
        return LLMCase(
            registry=build_llm_registry(
                repository=repository,
                models=models,
                manager_client=manager_client,
                compliance_client=compliance_client,
                call_log=call_log,
                source=library,
                tool_log=tool_log,
                verification_log=verification_log,
            ),
            manager_client=manager_client,
            compliance_client=compliance_client,
            log=call_log,
            source=library,
            tool_log=tool_log,
            verification_log=verification_log,
        )

    return _make


@pytest.fixture
def llm_case(make_llm_case) -> LLMCase:
    """The default model-backed scenario: both agents escalate."""
    return make_llm_case(
        manager_responses=(sample_case.manager_response_text(),),
        compliance_responses=(sample_case.compliance_response_text(),),
    )


@pytest.fixture
def llm_runner(workflow, conditions, llm_case) -> WorkflowRunner:
    return llm_case.runner(workflow, conditions)

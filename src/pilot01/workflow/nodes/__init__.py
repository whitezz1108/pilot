"""Workflow nodes and the v1 registries that wire them together.

Gate 1 ships an artifact loader for the Analyst and fixture-controlled doubles
for Manager and Compliance. Gate 2 adds real, model-backed Manager and
Compliance nodes behind an explicit model-client boundary. All of them implement
the same :class:`~pilot01.workflow.transitions.Node` interface, so the runner is
unchanged by the substitution.

**Two registries, and no default.** :func:`build_registry` wires the scripted
doubles; :func:`build_llm_registry` wires the real agents. Nothing chooses
between them -- a caller names one. There is no environment variable, no
fallback and no lazy default that could turn a deterministic test into a live
API call, and no code path in this repository constructs a model client
implicitly.
"""

from __future__ import annotations

from ...config import ModelsConfig
from ...model import ModelCallLog, ModelClient
from ...schemas import AnalystMemo, OmissionRecord
from ...source.tools import SourceLibrary, SourceTools, ToolCallLog
from ...source.verification import VerificationLog
from ..transitions import NodeRegistry
from .analyst import AnalystArtifactRequest, FrozenMemoRepository, make_analyst_node
from .compliance import (
    FakeCompliance,
    VerificationOutcome,
    build_compliance_messages,
    compliance_upstream_ids,
    make_compliance_node,
    make_llm_compliance_node,
)
from .manager import (
    FakeManager,
    build_manager_messages,
    make_llm_manager_node,
    make_manager_node,
    manager_upstream_ids,
)
from .script import FakeAgentScript

__all__ = [
    "AnalystArtifactRequest",
    "FrozenMemoRepository",
    "make_analyst_node",
    "FakeAgentScript",
    "FakeManager",
    "make_manager_node",
    "build_manager_messages",
    "manager_upstream_ids",
    "make_llm_manager_node",
    "FakeCompliance",
    "VerificationOutcome",
    "make_compliance_node",
    "build_compliance_messages",
    "compliance_upstream_ids",
    "make_llm_compliance_node",
    "build_registry",
    "build_v1_registry",
    "build_llm_registry",
    "build_llm_v1_registry",
]


def build_registry(
    *,
    repository: FrozenMemoRepository,
    manager_script: FakeAgentScript,
    compliance_script: FakeAgentScript,
) -> NodeRegistry:
    """The scripted v1 node set: analyst loader, fake manager, fake compliance.

    Both agent scripts are required. There is no default scenario, because a
    default scenario is a behavioural assumption wearing a convenient hat.
    """
    return NodeRegistry(
        [
            make_analyst_node(repository),
            make_manager_node(manager_script),
            make_compliance_node(compliance_script),
        ]
    )


def build_v1_registry(
    e0_memo: AnalystMemo,
    *,
    target_claim_id: str,
    manager_script: FakeAgentScript,
    compliance_script: FakeAgentScript,
    expected_category: str | None = None,
) -> tuple[NodeRegistry, OmissionRecord]:
    """Canonical scripted v1 wiring from a single correct memo.

    Derives the E1 omission arm from the E0 arm, so the two experimental arms
    are guaranteed to differ by exactly one deleted claim.
    """
    repository, provenance = FrozenMemoRepository.from_e0_memo(
        e0_memo, target_claim_id=target_claim_id, expected_category=expected_category
    )
    return (
        build_registry(
            repository=repository,
            manager_script=manager_script,
            compliance_script=compliance_script,
        ),
        provenance,
    )


def build_llm_registry(
    *,
    repository: FrozenMemoRepository,
    models: ModelsConfig,
    manager_client: ModelClient,
    compliance_client: ModelClient,
    call_log: ModelCallLog,
    source: SourceLibrary,
    tool_log: ToolCallLog,
    verification_log: VerificationLog,
) -> NodeRegistry:
    """The model-backed v1 node set: analyst loader, real manager, real compliance.

    Every collaborator is required and named:

    * ``models`` supplies each role's parameters, so the model id, temperature
      and output cap that a run used are the ones recorded in the call log;
    * ``manager_client`` and ``compliance_client`` are separate transports, so
      the two agents cannot share a session, a cache or a context;
    * ``call_log`` is mandatory, because an unrecorded model call is an
      unreproducible observation;
    * ``source``, ``tool_log`` and ``verification_log`` are mandatory for the
      same reason one level down: a source access that left no record is not an
      observation either.

    **One :class:`SourceTools` per node, one shared log.** The two agents do not
    share a tool object, so neither can read the other's ledger; they do share
    the :class:`ToolCallLog`, because a single ordered log with a ``node`` field
    is what makes the two histories comparable and lets either ledger be
    replayed from the record. Sharing the *log* while separating the *ledgers*
    is the whole of §6's node-specific access requirement.

    The Analyst is still the deterministic artifact loader. It is not a model
    call in this design: the frozen memo *is* the experimental intervention.
    """
    return NodeRegistry(
        [
            make_analyst_node(repository),
            make_llm_manager_node(
                client=manager_client,
                params=models.for_role("manager"),
                call_log=call_log,
                tools=SourceTools(node="manager", library=source, log=tool_log),
                verification_log=verification_log,
            )[0],
            make_llm_compliance_node(
                client=compliance_client,
                params=models.for_role("compliance"),
                call_log=call_log,
                tools=SourceTools(node="compliance", library=source, log=tool_log),
                verification_log=verification_log,
            )[0],
        ]
    )


def build_llm_v1_registry(
    e0_memo: AnalystMemo,
    *,
    target_claim_id: str,
    models: ModelsConfig,
    manager_client: ModelClient,
    compliance_client: ModelClient,
    call_log: ModelCallLog,
    source: SourceLibrary,
    tool_log: ToolCallLog,
    verification_log: VerificationLog,
    expected_category: str | None = None,
) -> tuple[NodeRegistry, OmissionRecord]:
    """Canonical model-backed v1 wiring from a single correct memo.

    Same arm derivation as :func:`build_v1_registry`; only the agent nodes
    differ.
    """
    repository, provenance = FrozenMemoRepository.from_e0_memo(
        e0_memo, target_claim_id=target_claim_id, expected_category=expected_category
    )
    return (
        build_llm_registry(
            repository=repository,
            models=models,
            manager_client=manager_client,
            compliance_client=compliance_client,
            call_log=call_log,
            source=source,
            tool_log=tool_log,
            verification_log=verification_log,
        ),
        provenance,
    )

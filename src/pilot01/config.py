"""Typed loaders for the versioned YAML configuration.

Four files, all versioned and all validated on load:

* ``config/conditions_v1.yaml`` -- the governance conditions
  (``source_access`` x ``verification_required``).
* ``config/workflow_v1.yaml`` -- the organizational workflow and its edge table.
* ``config/models_v1.yaml`` -- the model parameters per agent role.
* ``config/policies/policy_v1.yaml`` -- the organizational rule under test.

Validation here is deliberately strict. A configuration that would weaken
experimental validity should fail loudly at load time, not quietly at run time.

Nothing in this module constructs a model client or reads a credential. The
model file holds *parameters* -- provider label, model id, temperature, top_p,
output cap -- and never a key or a URL.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model.client import ModelParams
from .schemas import ClauseStatus, Decision, ExperimentalPolicy, expected_decision
from .workflow.transitions import (
    END,
    FeedbackLoopDisabledError,
    TransitionKind,
    UnknownTransitionError,
    WorkflowConfigError,
)

__all__ = [
    "ConditionConfig",
    "ExcludedCondition",
    "ConditionsConfig",
    "WorkflowConfig",
    "ModelsConfig",
    "PolicyConfig",
    "REQUIRED_MODEL_ROLES",
    "V1_MAIN_CONDITION_IDS",
    "V1_EXCLUDED_CONDITION_IDS",
    "config_dir",
    "policies_dir",
    "load_conditions_v1",
    "load_workflow_v1",
    "load_models_v1",
    "load_policy_v1",
    "policy_fingerprint",
]

V1_MAIN_CONDITION_IDS: tuple[str, ...] = ("A0V0", "A1V0", "A1V1")
"""The exact set of governance conditions permitted as main experimental cells."""

V1_EXCLUDED_CONDITION_IDS: tuple[str, ...] = ("A0V1",)
"""Conditions defined for the record but excluded from the main design."""

_CONFIG_DIR_ENV = "PILOT01_CONFIG_DIR"


def config_dir() -> Path:
    """Locate the ``config/`` directory.

    Honours ``PILOT01_CONFIG_DIR``; otherwise resolves relative to the
    repository layout (``<root>/config``).
    """
    override = os.environ.get(_CONFIG_DIR_ENV)
    if override:
        path = Path(override)
    else:
        path = Path(__file__).resolve().parents[2] / "config"
    if not path.is_dir():
        raise FileNotFoundError(
            f"config directory not found at {path!r}; set {_CONFIG_DIR_ENV} to override"
        )
    return path


def policies_dir() -> Path:
    """Locate the ``config/policies/`` directory.

    Policies live one level down from the other configuration because they are
    a different kind of artifact: the conditions, workflow and model files
    describe *how* the pilot runs, while a policy is the organizational rule
    the pilot is *about*, and it is the one configuration file an agent is
    shown the contents of. Keeping it separable makes that distinction visible
    in the tree.
    """
    path = config_dir() / "policies"
    if not path.is_dir():
        raise FileNotFoundError(
            f"policies directory not found at {path!r}; it should sit beside the "
            "other versioned configuration under config/"
        )
    return path


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise WorkflowConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


# --------------------------------------------------------------------------
# Governance conditions
# --------------------------------------------------------------------------


class ConditionConfig(BaseModel):
    """One governance cell: ``(source_access, verification_required)``.

    ``A0V1`` (no source access, verification required) is rejected here. It is
    a degenerate cell -- a verification obligation that can never be discharged
    -- and must not be run as a main experimental condition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_access: bool
    verification_required: bool
    description: str = ""

    @model_validator(mode="after")
    def _reject_degenerate_cell(self) -> "ConditionConfig":
        if self.verification_required and not self.source_access:
            raise ValueError(
                "invalid governance condition: verification_required=True with "
                "source_access=False is the excluded degenerate cell A0V1 and is "
                "not permitted as a main experimental condition"
            )
        return self


class ExcludedCondition(BaseModel):
    """A condition recorded in the config for auditability but never run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_access: bool
    verification_required: bool
    reason: str


class ConditionsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conditions_version: str
    conditions: dict[str, ConditionConfig]
    main_conditions: tuple[str, ...]
    excluded_conditions: dict[str, ExcludedCondition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_consistency(self) -> "ConditionsConfig":
        if len(set(self.main_conditions)) != len(self.main_conditions):
            raise WorkflowConfigError("main_conditions contains duplicates")

        missing = [cid for cid in self.main_conditions if cid not in self.conditions]
        if missing:
            raise WorkflowConfigError(
                f"main_conditions reference undefined conditions: {missing}"
            )

        overlapping = [cid for cid in self.main_conditions if cid in self.excluded_conditions]
        if overlapping:
            raise WorkflowConfigError(
                f"conditions listed as both main and excluded: {overlapping}"
            )
        return self

    def get(self, condition_id: str) -> ConditionConfig:
        try:
            return self.conditions[condition_id]
        except KeyError:
            raise KeyError(
                f"unknown governance condition {condition_id!r}; "
                f"defined: {sorted(self.conditions)}"
            ) from None

    def is_main(self, condition_id: str) -> bool:
        return condition_id in self.main_conditions


def load_conditions_v1(path: Path | None = None) -> ConditionsConfig:
    """Load and validate ``conditions_v1.yaml``.

    Additionally pins the main-cell set to :data:`V1_MAIN_CONDITION_IDS` so the
    v1 design cannot drift by editing the YAML alone.
    """
    resolved = path or (config_dir() / "conditions_v1.yaml")
    config = ConditionsConfig.model_validate(_load_yaml(resolved))

    actual = tuple(sorted(config.main_conditions))
    expected = tuple(sorted(V1_MAIN_CONDITION_IDS))
    if actual != expected:
        raise WorkflowConfigError(
            f"{resolved.name} must define exactly the v1 main conditions {expected}; got {actual}"
        )
    return config


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------


class WorkflowConfig(BaseModel):
    """Workflow definition: nodes plus the authoritative edge table.

    Invariants enforced at load time:

    * ``start`` is a declared node, and every declared node has a ``default``
      edge;
    * every edge target is a declared node or :data:`END`;
    * a ``request_revision`` edge may only exist when
      ``max_revision_rounds > 0`` -- a config cannot declare a feedback route
      that the run is not allowed to take.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_version: str
    start: str
    nodes: tuple[str, ...]
    edges: dict[str, dict[TransitionKind, str]]
    max_revision_rounds: int = Field(ge=0)
    max_steps: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_graph(self) -> "WorkflowConfig":
        if len(set(self.nodes)) != len(self.nodes):
            raise WorkflowConfigError("nodes contains duplicates")
        if self.start not in self.nodes:
            raise WorkflowConfigError(f"start node {self.start!r} is not in nodes {self.nodes}")
        if self.max_steps < len(self.nodes):
            raise WorkflowConfigError(
                f"max_steps={self.max_steps} is smaller than the node count {len(self.nodes)}"
            )

        known_targets = set(self.nodes) | {END}
        for node in self.nodes:
            table = self.edges.get(node)
            if table is None:
                raise WorkflowConfigError(f"node {node!r} has no edge table")
            if TransitionKind.DEFAULT not in table:
                raise WorkflowConfigError(f"node {node!r} has no 'default' edge")
            for kind, target in table.items():
                if target not in known_targets:
                    raise WorkflowConfigError(
                        f"edge {node!r}.{kind.value} points at unknown target {target!r}"
                    )
                if kind is TransitionKind.REQUEST_REVISION and self.max_revision_rounds < 1:
                    raise WorkflowConfigError(
                        f"node {node!r} declares a 'request_revision' edge but "
                        f"max_revision_rounds={self.max_revision_rounds}; the feedback "
                        "loop must not be declared while it is disabled"
                    )

        unknown_nodes = set(self.edges) - set(self.nodes)
        if unknown_nodes:
            raise WorkflowConfigError(f"edge table defines undeclared nodes: {sorted(unknown_nodes)}")
        return self

    def resolve(self, node: str, kind: TransitionKind) -> str:
        """Resolve the destination for a transition intent.

        Raises :class:`UnknownTransitionError` if the node has no edge for
        ``kind``, and :class:`FeedbackLoopDisabledError` if a revision is
        requested while ``max_revision_rounds`` is 0.
        """
        if kind is TransitionKind.REQUEST_REVISION and self.max_revision_rounds < 1:
            raise FeedbackLoopDisabledError(
                f"node {node!r} requested a revision but max_revision_rounds="
                f"{self.max_revision_rounds}; the feedback loop is disabled in "
                f"workflow version {self.workflow_version}"
            )
        table = self.edges.get(node)
        if table is None:
            raise UnknownTransitionError(f"unknown node {node!r}")
        if kind not in table:
            raise UnknownTransitionError(
                f"node {node!r} has no {kind.value!r} transition; "
                f"available: {sorted(k.value for k in table)}"
            )
        return table[kind]


def load_workflow_v1(path: Path | None = None) -> WorkflowConfig:
    """Load and validate ``workflow_v1.yaml``."""
    resolved = path or (config_dir() / "workflow_v1.yaml")
    return WorkflowConfig.model_validate(_load_yaml(resolved))


# --------------------------------------------------------------------------
# Model parameters
# --------------------------------------------------------------------------

REQUIRED_MODEL_ROLES: tuple[str, ...] = ("manager", "compliance")
"""Roles that must have model parameters. A missing role fails at load time."""


class ModelsConfig(BaseModel):
    """Per-role model parameters, validated on load.

    Reuses :class:`~pilot01.model.client.ModelParams` rather than defining a
    parallel parameter model, so what the config declares and what the raw-call
    log records are the same object.

    There is no default role and no default parameter set. A live run must say
    which model it is using, because "the model" is an experimental variable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    models_version: str
    roles: dict[str, ModelParams]

    @model_validator(mode="after")
    def _require_roles(self) -> "ModelsConfig":
        missing = [role for role in REQUIRED_MODEL_ROLES if role not in self.roles]
        if missing:
            raise WorkflowConfigError(
                f"models config declares no parameters for role(s) {missing}; "
                f"required roles are {list(REQUIRED_MODEL_ROLES)}"
            )
        extra = sorted(set(self.roles) - set(REQUIRED_MODEL_ROLES))
        if extra:
            raise WorkflowConfigError(
                f"models config declares unknown role(s) {extra}; "
                f"known roles are {list(REQUIRED_MODEL_ROLES)}"
            )
        return self

    def for_role(self, role: str) -> ModelParams:
        try:
            return self.roles[role]
        except KeyError:
            raise KeyError(
                f"no model parameters for role {role!r}; declared: {sorted(self.roles)}"
            ) from None


def load_models_v1(path: Path | None = None) -> ModelsConfig:
    """Load and validate ``models_v1.yaml``.

    Reads parameters only. No credential, no endpoint, no client.
    """
    resolved = path or (config_dir() / "models_v1.yaml")
    return ModelsConfig.model_validate(_load_yaml(resolved))


# --------------------------------------------------------------------------
# The policy under test
# --------------------------------------------------------------------------


class PolicyConfig(BaseModel):
    """``policy_v1.yaml``: the organizational rule, with its rationale.

    Loaded rather than hard-coded because the rule is an experimental design
    input. The clause categories POLICY-01 treats as critical are the audited
    dataset decision that makes a case constructible at all, and a rule that
    lived as a Python default would let a placeholder silently become the
    policy under test -- which is exactly why
    :class:`~pilot01.schemas.ExperimentalPolicy` requires them.

    ``clause_status_mapping`` is checked against ``expected_decision`` on load.
    The file states the mapping exhaustively for a reader; the function is the
    implementation. If they disagree, one of them is wrong and the run would
    score against a rule it did not state, so the disagreement is a load error
    rather than something a later reader has to notice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    policy_version: str
    policy_status: str = "development"
    """``development`` or ``confirmatory``. Gate 4 writes ``development``.

    Carried explicitly so that a Gate-4 artifact cannot be mistaken later for a
    confirmatory one by someone reading only the file.
    """

    target_clause_categories: tuple[str, ...] = Field(min_length=1)
    decision_if_target_present: Decision
    decision_if_target_absent: Decision
    description: str

    clause_status_mapping: dict[ClauseStatus, Decision]
    edge_cases: tuple[dict, ...] = ()

    @model_validator(mode="after")
    def _check_mapping(self) -> "PolicyConfig":
        policy = self.to_policy()
        for status, mapped in self.clause_status_mapping.items():
            reference = expected_decision(status, policy)
            if mapped is not reference:
                raise WorkflowConfigError(
                    f"policy {self.policy_id!r} maps clause status {status.value!r} to "
                    f"{mapped.value}, but the reference implementation of the rule maps "
                    f"it to {reference.value}; the stated rule and the scored rule must "
                    "be the same rule"
                )
        missing = [s for s in ClauseStatus if s not in self.clause_status_mapping]
        if missing:
            raise WorkflowConfigError(
                f"policy {self.policy_id!r} does not state a mapping for clause "
                f"status(es) {[s.value for s in missing]}; an unstated status is one a "
                "reader has to infer, and the edge cases are the point of the rule"
            )
        return self

    def to_policy(self) -> ExperimentalPolicy:
        """The core policy object the workflow renders and the scorer applies."""
        return ExperimentalPolicy(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            target_clause_categories=tuple(self.target_clause_categories),
            decision_if_target_present=self.decision_if_target_present,
            decision_if_target_absent=self.decision_if_target_absent,
            description=self.description,
        )

    def fingerprint(self) -> str:
        """A digest over the policy's *semantics*, not its file bytes.

        Deliberately not a hash of the YAML: comments, key order and the
        rationale prose are documentation, and a fingerprint that moved when a
        typo in a comment was fixed would train a reader to ignore it. What is
        hashed is what changes the experiment -- the rule, its triggers, and the
        mapping it implies.
        """
        material = json.dumps(
            {
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "target_clause_categories": list(self.target_clause_categories),
                "decision_if_target_present": self.decision_if_target_present.value,
                "decision_if_target_absent": self.decision_if_target_absent.value,
                "description": self.description,
                "clause_status_mapping": {
                    status.value: decision.value
                    for status, decision in sorted(
                        self.clause_status_mapping.items(), key=lambda kv: kv[0].value
                    )
                },
            },
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_policy_v1(path: Path | None = None) -> PolicyConfig:
    """Load and validate ``config/policies/policy_v1.yaml``.

    Reads the rule and its rationale. No credential, no endpoint, no client,
    and no gold label: the policy names its triggers, never a case's status.
    """
    resolved = path or (policies_dir() / "policy_v1.yaml")
    return PolicyConfig.model_validate(_load_yaml(resolved))


def policy_fingerprint(policy: PolicyConfig) -> str:
    """The fingerprint of a loaded policy, for a plan or registry header."""
    return policy.fingerprint()

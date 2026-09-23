"""pilot01 -- orchestration skeleton for the legal multi-agent
omission-propagation pilot (Gates 1, 1.5 and 2).

The conceptual workflow is

    frozen analyst memo -> Manager -> Compliance -> final decision

and is strictly sequential in v1. The Analyst is a deterministic artifact
loader; Manager and Compliance each have two implementations, a scripted
fixture double and a real model-backed agent behind
:class:`~pilot01.model.client.ModelClient`. Nothing here constructs a model
client implicitly, and no test reaches the network. See ``README.md`` for what
each gate does and does not cover.
"""

from .config import (
    ConditionsConfig,
    ConditionConfig,
    ModelsConfig,
    WorkflowConfig,
    load_conditions_v1,
    load_models_v1,
    load_workflow_v1,
)
from .events import Event, EventCollector, EventType
from .model import (
    ModelCallLog,
    ModelCallRecord,
    ModelClient,
    ModelClientError,
    ModelMessage,
    ModelOutputError,
    ModelParams,
    ModelRequest,
    ModelResponse,
    ScriptedModelClient,
)
from .prompts import PromptTemplate, load_prompt
from .schemas import (
    AnalystMemo,
    ClauseStatus,
    ComplianceOutput,
    Decision,
    ErrorCondition,
    ExperimentalPolicy,
    ManagerOutput,
    MemoClaim,
    VerificationStatus,
    build_omission_memo,
    policy_01,
)
from .workflow.export import (
    ExecutionRecord,
    ExportLeakError,
    ScoringRecord,
    build_execution_record,
    build_scoring_record,
)

__version__ = "0.1.0"

__all__ = [
    "AnalystMemo",
    "ClauseStatus",
    "ComplianceOutput",
    "ConditionConfig",
    "ConditionsConfig",
    "Decision",
    "ErrorCondition",
    "Event",
    "EventCollector",
    "EventType",
    "ExecutionRecord",
    "ExperimentalPolicy",
    "ExportLeakError",
    "ManagerOutput",
    "MemoClaim",
    "ModelCallLog",
    "ModelCallRecord",
    "ModelClient",
    "ModelClientError",
    "ModelMessage",
    "ModelOutputError",
    "ModelParams",
    "ModelRequest",
    "ModelResponse",
    "ModelsConfig",
    "PromptTemplate",
    "ScoringRecord",
    "ScriptedModelClient",
    "VerificationStatus",
    "WorkflowConfig",
    "build_execution_record",
    "build_omission_memo",
    "build_scoring_record",
    "load_conditions_v1",
    "load_models_v1",
    "load_prompt",
    "load_workflow_v1",
    "policy_01",
    "__version__",
]

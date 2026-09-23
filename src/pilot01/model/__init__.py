"""The model-call boundary.

Nothing in :mod:`pilot01.workflow.nodes` talks to a provider SDK. An agent node
renders its restricted view into messages, hands a
:class:`~pilot01.model.client.ModelRequest` to a
:class:`~pilot01.model.client.ModelClient`, and gets back a
:class:`~pilot01.model.client.ModelResponse`. Provider specifics -- HTTP, auth,
retries, response envelopes -- live behind that protocol and nowhere else.

The four modules:

* :mod:`~pilot01.model.client` -- the protocol, the request/response models, the
  parameter record, and the two error types.
* :mod:`~pilot01.model.scripted` -- a deterministic transport double. This is
  what tests use, and it is the reason no test needs the network.
* :mod:`~pilot01.model.parsing` -- raw text to a validated domain object, with
  at most one format-only repair attempt.
* :mod:`~pilot01.model.log` -- the raw-call log: a separate, private, append-only
  record of what was actually sent and received.

The split from :mod:`pilot01.workflow.export` is deliberate. ``ExecutionRecord``
is the *public* experimental record and is gold-free by construction;
``ModelCallRecord`` is the *private* raw request/response log and is neither
public nor exportable. They are separate types with separate fields, so neither
can be mistaken for the other.
"""

from __future__ import annotations

from .client import (
    ModelClient,
    ModelClientError,
    ModelMessage,
    ModelOutputError,
    ModelParams,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from .log import ModelCallLog, ModelCallRecord, SecretLeakError
from .parsing import (
    MAX_FORMAT_REPAIRS,
    RepairSpec,
    StructuredCallResult,
    call_structured,
    extract_json_object,
    parse_structured,
    record_call,
)
from .scripted import ScriptedModelClient

__all__ = [
    "ModelClient",
    "ModelClientError",
    "ModelMessage",
    "ModelOutputError",
    "ModelParams",
    "ModelRequest",
    "ModelResponse",
    "TokenUsage",
    "ScriptedModelClient",
    "MAX_FORMAT_REPAIRS",
    "RepairSpec",
    "StructuredCallResult",
    "call_structured",
    "extract_json_object",
    "parse_structured",
    "record_call",
    "ModelCallLog",
    "ModelCallRecord",
    "SecretLeakError",
]

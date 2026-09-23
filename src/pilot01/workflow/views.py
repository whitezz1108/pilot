"""Restricted, agent-facing input views.

This is the primary experimental-validity boundary of the codebase. An agent
node must never receive :class:`~pilot01.workflow.state.ExperimentState`. It
receives a view built here by an explicit whitelist.

Three independent protections, so that no single mistake leaks the treatment:

1. **Structural.** Views are frozen pydantic models with ``extra="forbid"``.
   :class:`RestrictedView` refuses *at class-definition time* to declare a
   field whose name looks like hidden data, so a future edit cannot add
   ``gold_action`` to ``ManagerInput`` and have it silently work.
2. **Construction.** :func:`build_manager_view` and
   :func:`build_compliance_view` copy named attributes one by one. Nothing is
   spread, splatted, or passed through.
3. **Runtime audit.** :func:`assert_view_clean` walks the *serialized* view and
   raises if any key anywhere in it matches a forbidden pattern. The runner
   calls it before dispatching to any node, and the tests call it directly.

Role isolation is intentional and asymmetric:

* ``ManagerInput`` may contain the analyst memo.
* ``ComplianceInput`` may **not** -- Compliance sees only the Manager's
  handoff. It also never sees Manager's internal execution history, prior
  message history, or treatment labels.

Rule D (fresh context per agent call) is expressed structurally: each call gets
a newly constructed, frozen view carrying an ``invocation`` index, and no
message-history object exists anywhere in the codebase.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import AnalystMemo, ExperimentalPolicy, ManagerOutput
from .state import ExperimentState
from .transitions import ProtocolError

__all__ = [
    "ViewLeakError",
    "FORBIDDEN_KEY_SUBSTRINGS",
    "FORBIDDEN_KEY_TOKENS",
    "RestrictedView",
    "ManagerInput",
    "ComplianceInput",
    "build_manager_view",
    "build_compliance_view",
    "iter_keys",
    "audit_keys",
    "audit_view",
    "assert_view_clean",
]


class ViewLeakError(ProtocolError):
    """A restricted view contains hidden experimental data."""


FORBIDDEN_KEY_SUBSTRINGS: tuple[str, ...] = (
    "gold",
    "hidden",
    "omission",
    "omit",
    "error_condition",
    "condition_id",
    "treatment",
    "injected",
    "leak",
)

FORBIDDEN_KEY_TOKENS: tuple[str, ...] = ("e0", "e1")
"""Matched as whole ``_``-separated tokens, so ``case_id`` is not flagged."""


def _key_violations(key: str) -> tuple[str, ...]:
    lowered = key.lower()
    hits = [pattern for pattern in FORBIDDEN_KEY_SUBSTRINGS if pattern in lowered]
    tokens = set(lowered.split("_"))
    hits.extend(f"token:{token}" for token in FORBIDDEN_KEY_TOKENS if token in tokens)
    return tuple(hits)


class RestrictedView(BaseModel):
    """Base class for every agent-facing input.

    Frozen (an agent cannot mutate its own instructions), closed
    (``extra="forbid"``), and self-policing about field names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ClassVar, not a field: this is a declaration about the class, and must
    # not itself appear in the serialized view.
    forbidden_key_patterns: ClassVar[tuple[str, ...]] = ()
    """Extra substrings this particular view forbids in its own field names."""

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name in cls.model_fields:
            violations = list(_key_violations(name))
            violations.extend(
                pattern for pattern in cls.forbidden_key_patterns if pattern in name.lower()
            )
            if violations:
                raise ViewLeakError(
                    f"{cls.__name__} declares field {name!r}, which matches forbidden "
                    f"hidden-data pattern(s) {violations}; restricted views must not "
                    f"carry hidden experimental data"
                )


class ManagerInput(RestrictedView):
    """Exactly what the Manager agent may observe.

    Permitted: case identifier, experimental policy, frozen analyst memo, and
    the two governance flags. Nothing else.
    """

    case_id: str
    policy: ExperimentalPolicy
    analyst_memo: AnalystMemo
    source_access: bool
    verification_required: bool
    invocation: int = Field(default=0, ge=0, description="Fresh-context index for this call.")


class ComplianceInput(RestrictedView):
    """Exactly what the Compliance agent may observe.

    Permitted: case identifier, experimental policy, the Manager handoff, and
    the two governance flags.

    Deliberately excluded: the analyst memo (Compliance must work from the
    Manager's handoff alone), Manager's internal execution history, any prior
    message history, and all treatment labels.
    """

    forbidden_key_patterns: ClassVar[tuple[str, ...]] = (
        "memo",
        "analyst",
        "manager_input",
        "history",
    )

    case_id: str
    policy: ExperimentalPolicy
    manager_output: ManagerOutput
    source_access: bool
    verification_required: bool
    invocation: int = Field(default=0, ge=0, description="Fresh-context index for this call.")


def build_manager_view(state: ExperimentState, *, invocation: int = 0) -> ManagerInput:
    """Build the Manager's restricted view from the full state.

    Explicit field-by-field copy: the whitelist is the code.
    """
    return ManagerInput(
        case_id=state.case_id,
        policy=state.policy,
        analyst_memo=state.analyst_memo,
        source_access=state.source_access,
        verification_required=state.verification_required,
        invocation=invocation,
    )


def build_compliance_view(state: ExperimentState, *, invocation: int = 0) -> ComplianceInput:
    """Build the Compliance view from the full state.

    Fails closed if the Manager has not yet produced a handoff. Note that the
    analyst memo is *not* read here at all -- not merely omitted from the
    model -- so there is no code path by which it could reach Compliance.
    """
    if state.manager_output is None:
        raise ProtocolError(
            f"cannot build a Compliance view for run {state.run_id!r}: no manager output"
        )
    return ComplianceInput(
        case_id=state.case_id,
        policy=state.policy,
        manager_output=state.manager_output,
        source_access=state.source_access,
        verification_required=state.verification_required,
        invocation=invocation,
    )


def iter_keys(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(key_path, key_name)`` for every mapping key in a serialized model.

    Shared with :mod:`pilot01.workflow.export`, which scans the public execution
    record for hidden keys the same way.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, str(key)
            yield from iter_keys(nested, path)
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            yield from iter_keys(nested, f"{prefix}[{index}]")


def audit_keys(value: Any, *, extra_forbidden: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return the key paths in any *serialized* object that look like hidden data.

    The same scan as :func:`audit_view`, but for objects that are not
    ``RestrictedView`` subclasses -- notably the model request a node is about
    to send to a provider, which must be held to the same standard as the view
    it was rendered from.
    """
    violations: list[str] = []
    for path, key in iter_keys(value):
        hits = list(_key_violations(key))
        hits.extend(pattern for pattern in extra_forbidden if pattern in key.lower())
        if hits:
            violations.append(f"{path} ({', '.join(hits)})")
    return tuple(violations)


def audit_view(view: RestrictedView, *, extra_forbidden: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return the key paths in a *serialized* view that look like hidden data.

    Works on ``model_dump(mode="json")`` rather than on ``model_fields`` so it
    catches data that arrived nested inside a permitted field.
    """
    return audit_keys(view.model_dump(mode="json"), extra_forbidden=extra_forbidden)


def assert_view_clean(
    view: RestrictedView, *, extra_forbidden: tuple[str, ...] = ()
) -> None:
    """Raise :class:`ViewLeakError` if a serialized view carries hidden data."""
    violations = audit_view(view, extra_forbidden=extra_forbidden)
    if violations:
        raise ViewLeakError(
            f"{type(view).__name__} leaks hidden experimental data at: "
            f"{'; '.join(violations)}"
        )

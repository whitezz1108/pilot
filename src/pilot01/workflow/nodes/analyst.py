"""The Analyst node: a deterministic artifact loader, **not** an LLM call.

The Analyst is conceptually an agent role, but during the formal experiment it
is never called as a model. Every contract already has a frozen memo artifact,
and this node's only job is to select the right one:

* ``E0`` -> the correct frozen memo;
* ``E1`` -> the deterministic omission version of that same memo, produced
  offline by :func:`pilot01.schemas.build_omission_memo`.

Because selecting the artifact *is* the experimental intervention, this node is
deliberately given the hidden error condition. It therefore does **not** receive
a :class:`~pilot01.workflow.views.RestrictedView` -- and, by the same token, its
input model must never be handed to Manager or Compliance. The runner enforces
that separation by node type (see :class:`~pilot01.workflow.transitions.Node`).
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from ...schemas import AnalystMemo, ErrorCondition, OmissionRecord, build_omission_memo
from ..transitions import Node, NodeResult, ProtocolError

__all__ = [
    "AnalystArtifactRequest",
    "FrozenMemoRepository",
    "make_analyst_node",
]


class AnalystArtifactRequest(BaseModel):
    """Input to the analyst *artifact loader*.

    Carries the hidden error condition on purpose (see module docstring). This
    is not a restricted view and must never reach an agent node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    error_condition: ErrorCondition


class FrozenMemoRepository:
    """Maps ``(case_id, error_condition)`` to a frozen memo.

    A plain in-memory lookup: no retrieval, no ranking, no embeddings, no
    network. The E1 arm is always derived from the E0 arm by
    :meth:`from_e0_memo`, which is what guarantees the two arms differ by
    exactly one deleted claim.
    """

    def __init__(self, memos: Mapping[tuple[str, ErrorCondition], AnalystMemo]) -> None:
        self._memos = dict(memos)

    @classmethod
    def from_e0_memo(
        cls,
        memo: AnalystMemo,
        *,
        target_claim_id: str,
        expected_category: str | None = None,
    ) -> tuple["FrozenMemoRepository", OmissionRecord]:
        """Build both arms from one correct memo.

        Returns the repository and the hidden omission provenance record. The
        provenance record is experimenter-only; the E1 memo it describes is
        indistinguishable from E0 apart from the missing claim.
        """
        omission = build_omission_memo(
            memo, target_claim_id, expected_category=expected_category
        )
        repository = cls(
            {
                (memo.case_id, ErrorCondition.E0): memo,
                (memo.case_id, ErrorCondition.E1): omission.memo,
            }
        )
        return repository, omission.provenance

    def load(self, case_id: str, error_condition: ErrorCondition) -> AnalystMemo:
        try:
            return self._memos[(case_id, error_condition)]
        except KeyError:
            raise ProtocolError(
                f"no frozen memo for case {case_id!r} / condition "
                f"{error_condition.value}; repository holds "
                f"{sorted((cid, cond.value) for cid, cond in self._memos)}"
            ) from None

    def __len__(self) -> int:
        return len(self._memos)


def make_analyst_node(repository: FrozenMemoRepository) -> Node:
    """Build the analyst node bound to a frozen-artifact repository."""

    def build_input(state, invocation: int) -> AnalystArtifactRequest:
        return AnalystArtifactRequest(
            case_id=state.case_id,
            error_condition=state.error_condition,
        )

    def run(request: AnalystArtifactRequest) -> NodeResult:
        if not isinstance(request, AnalystArtifactRequest):
            raise ProtocolError(
                f"analyst node received {type(request).__name__}; expected "
                "AnalystArtifactRequest"
            )
        memo = repository.load(request.case_id, request.error_condition)
        return NodeResult(output=memo)

    def apply(state, output) -> None:
        """Loading is a read: it may only confirm the run's frozen artifact.

        The memo is fixed when the run is constructed, so a mismatch means the
        repository and the state disagree -- which would invalidate the run.
        """
        if output != state.analyst_memo:
            raise ProtocolError(
                f"analyst node loaded memo {getattr(output, 'memo_id', output)!r} but "
                f"run {state.run_id!r} is frozen to {state.analyst_memo.memo_id!r}"
            )

    return Node(
        name="analyst",
        build_input=build_input,
        run=run,
        apply=apply,
        requires_restricted_view=False,
        output_model=AnalystMemo,
    )

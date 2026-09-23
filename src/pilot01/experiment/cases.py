"""The case registry: what an experimental case *is*, and where its scoring keys live.

A case is the unit the experiment is about. It bundles four things that must
travel together and must never be mixed up:

* the **contract** the run is about, as immutable text;
* the **correct (E0) analyst memo** over that contract, from which the E1
  omission arm is derived deterministically;
* the **case class** -- the target clause category, whether this is a negative
  sentinel, and which error conditions the case is run under;
* the **scoring keys** -- the gold clause status and the gold evidence span.

The first two are agent-facing in the sense that they reach a run; the last two
are not. Gold lives *here*, in the experiment layer, and the only two things
that read it are :meth:`ExperimentState.create` (which is the run's own hidden
record, per Gate 1) and the scoring layer. Nothing in
:mod:`pilot01.workflow.nodes`, :mod:`pilot01.source` or
:mod:`pilot01.model` imports this module, and a test asserts that.

**Why the registry is a file and not a directory of loose artifacts.** The
registry is the frozen experimental input: it names the cases, fixes their
contract text, their memos and their labels, and pins the chunking that turns
contract text into addressable paragraphs. Two runs that read the same registry
file are running the same experiment. Changing a case means changing the file,
which is a visible act rather than an edit to a fixture.

**No cases ship here.** Gate 3 builds the machinery and tests it on synthetic
cases; choosing the real development contracts is a later gate's job, and the
registry format is what that gate will write.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schemas import (
    AnalystMemo,
    ClauseStatus,
    Decision,
    ErrorCondition,
    ExperimentalPolicy,
    MemoClaim,
    OmissionRecord,
    build_omission_memo,
    expected_decision,
)
from ..source import ContractDocument, SourceLibrary, sha256_text
from ..workflow.nodes import FrozenMemoRepository

__all__ = [
    "REGISTRY_VERSION",
    "CaseRegistryError",
    "CaseSpec",
    "CaseRegistry",
    "gold_offsets_for_paragraph",
    "is_safe_component",
]


REGISTRY_VERSION = "1"
"""Version of the on-disk registry format. Bumped when its shape changes."""


class CaseRegistryError(RuntimeError):
    """A registry is internally inconsistent, or cannot be read."""


def is_safe_component(value: str) -> bool:
    """Whether a string can be used as a path segment and a run-id component.

    Case, contract and experiment identifiers all end up inside a run id, and a
    run id is a directory name. A component carrying a path separator, a ``..``,
    whitespace or a control character would either escape the artifact root or
    produce a name that cannot be read back reliably, so the registry refuses it
    at construction rather than discovering it while writing a file.
    """
    if not value or value != value.strip():
        return False
    if value in {".", ".."}:
        return False
    if any(character in value for character in "/\\\0"):
        return False
    return not any(character.isspace() for character in value)


class CaseSpec(BaseModel):
    """One experimental case.

    ``error_conditions`` is required and has no default, for the same reason
    :class:`~pilot01.schemas.ExperimentalPolicy` requires its target categories:
    which arms a case is run under is an experimental design decision. A paired
    case declares ``(E0, E1)`` and produces both arms from one memo; a negative
    sentinel whose target clause is genuinely absent may legitimately declare
    ``(E0,)`` alone, because there is no present clause for an omission to
    delete.

    ``is_negative_sentinel`` is likewise declared rather than derived, and is
    then *checked* against the gold action. A case that says it is a sentinel
    while its gold action is ESCALATE is a design error, and it fails here
    rather than silently reclassifying itself during scoring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    contract_id: str
    contract_text: str
    """The contract, verbatim. Read and sliced by the source layer, never written to."""

    target_category: str
    policy: ExperimentalPolicy

    memo: AnalystMemo
    """The correct (E0) memo. The E1 arm is derived from it, never written out."""

    gold_clause_status: ClauseStatus
    gold_evidence_offsets: tuple[int, ...] = ()
    """Consecutive ``(start, end)`` pairs into the contract text. May be empty.

    Empty means "this case has no gold evidence span", which is a legitimate
    state for a sentinel and is reported as such by the evidence metric rather
    than being treated as a failure to overlap.
    """

    is_negative_sentinel: bool
    error_conditions: tuple[ErrorCondition, ...]
    omission_target_claim_id: str | None = None
    """Which claim the E1 arm removes. Required exactly when E1 is run."""

    omission_replacement_claim: MemoClaim | None = None
    """What takes the target's place in E1, or ``None`` for a plain deletion.

    A substitution keeps the two arms the same size, which removes memo length
    as a cue that something is missing -- worth having when the alternative is
    an E1 memo that is systematically one claim shorter than every E0 memo.
    The claim must be one the E0 memo does not already hold, and it must sit in
    a category POLICY-01 does not treat as a target, because a replacement that
    was itself a policy trigger would leave the case with the trigger it was
    supposed to lose. Both are checked below rather than trusted: this field is
    the manipulation, and a mis-specified manipulation is not a finding about
    an agent.
    """

    @model_validator(mode="after")
    def _check_case(self) -> "CaseSpec":
        for name, value in (("case_id", self.case_id), ("contract_id", self.contract_id)):
            if not is_safe_component(value):
                raise CaseRegistryError(
                    f"{name} {value!r} is not usable as a path segment; identifiers "
                    "must be non-empty, free of whitespace, path separators and "
                    "'..', because they become run-id components and directory names"
                )
        if not self.contract_text.strip():
            raise CaseRegistryError(f"case {self.case_id!r} has no contract text")
        if not self.target_category.strip():
            raise CaseRegistryError(f"case {self.case_id!r} has a blank target category")

        if self.memo.case_id != self.case_id:
            raise CaseRegistryError(
                f"case {self.case_id!r} carries memo {self.memo.memo_id!r} for case "
                f"{self.memo.case_id!r}"
            )

        if not self.error_conditions:
            raise CaseRegistryError(
                f"case {self.case_id!r} declares no error conditions; a case that is "
                "never run is not a case"
            )
        if len(set(self.error_conditions)) != len(self.error_conditions):
            raise CaseRegistryError(
                f"case {self.case_id!r} lists duplicate error conditions "
                f"{[c.value for c in self.error_conditions]}"
            )

        sentinel = self.gold_action is Decision.ACCEPT
        if self.is_negative_sentinel is not sentinel:
            raise CaseRegistryError(
                f"case {self.case_id!r} declares is_negative_sentinel="
                f"{self.is_negative_sentinel} but its gold action is "
                f"{self.gold_action.value}; a sentinel is exactly a case whose gold "
                "action is ACCEPT"
            )

        if len(self.gold_evidence_offsets) % 2:
            raise CaseRegistryError(
                f"case {self.case_id!r} declares {len(self.gold_evidence_offsets)} gold "
                "offset value(s); offsets are consecutive (start, end) pairs, so the "
                "sequence must have even length"
            )

        runs_e1 = ErrorCondition.E1 in self.error_conditions
        if runs_e1:
            if not self.omission_target_claim_id:
                raise CaseRegistryError(
                    f"case {self.case_id!r} runs the omission arm but names no "
                    "omission_target_claim_id; the E1 arm removes exactly one named claim"
                )
            claim = self.memo.claim(self.omission_target_claim_id)
            if claim is None:
                raise CaseRegistryError(
                    f"case {self.case_id!r} omits claim "
                    f"{self.omission_target_claim_id!r}, which is not in memo "
                    f"{self.memo.memo_id!r}; available: {self.memo.claim_ids()!r}"
                )
            if claim.category != self.target_category:
                raise CaseRegistryError(
                    f"case {self.case_id!r} omits claim "
                    f"{self.omission_target_claim_id!r} of category {claim.category!r}, "
                    f"which is not the case target category {self.target_category!r}"
                )
            if self.omission_replacement_claim is not None:
                replacement = self.omission_replacement_claim
                if self.memo.claim(replacement.claim_id) is not None:
                    raise CaseRegistryError(
                        f"case {self.case_id!r} replaces the omitted claim with "
                        f"{replacement.claim_id!r}, which the E0 memo already holds; a "
                        "substitution adds no claim, so the replacement must be new to "
                        "the memo"
                    )
                if self.policy.is_target_category(replacement.category):
                    raise CaseRegistryError(
                        f"case {self.case_id!r} replaces the omitted claim with one of "
                        f"category {replacement.category!r}, which POLICY-01 treats as a "
                        "target; the case would still carry a policy trigger after the "
                        "manipulation, so the omission would not be the only thing that "
                        "changed"
                    )
        elif self.omission_target_claim_id is not None:
            raise CaseRegistryError(
                f"case {self.case_id!r} does not run the omission arm but names an "
                f"omission target ({self.omission_target_claim_id!r}); the target would "
                "never be used, so its presence is a registry mistake"
            )
        elif self.omission_replacement_claim is not None:
            raise CaseRegistryError(
                f"case {self.case_id!r} does not run the omission arm but declares an "
                f"omission replacement ({self.omission_replacement_claim.claim_id!r}); "
                "there is no omission for it to take the place of"
            )
        return self

    # -- derived -----------------------------------------------------------

    @property
    def gold_action(self) -> Decision:
        """The decision POLICY-01 requires, derived from the gold clause status.

        Derived rather than stored: :class:`~pilot01.workflow.state.ExperimentState`
        already refuses a gold label whose action disagrees with the policy, so a
        second, independently-declared copy could only ever disagree with it.
        """
        return expected_decision(self.gold_clause_status, self.policy)

    @property
    def contract_text_hash(self) -> str:
        """The digest of the contract text, computed here rather than declared."""
        return f"sha256:{sha256_text(self.contract_text)}"

    def gold_spans(self) -> tuple[tuple[int, int], ...]:
        """The gold evidence offsets as half-open ``[start, end)`` spans."""
        offsets = self.gold_evidence_offsets
        return tuple(
            (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
        )

    def runs(self, error_condition: ErrorCondition) -> bool:
        return error_condition in self.error_conditions


def gold_offsets_for_paragraph(document: ContractDocument, paragraph_id: str) -> tuple[int, int]:
    """The ``[start, end)`` span of one paragraph, for use as a case's gold evidence.

    A convenience for building synthetic cases: the gold evidence for "the clause
    this case is about" is the span of the paragraph that states it, so the
    evidence-overlap metric compares the paragraph a node opened against the
    paragraph the case is about. Real cases will supply offsets derived from the
    CUAD annotations instead; this helper exists so a fixture does not have to
    hard-code offsets that a chunking change would silently invalidate.
    """
    paragraph = document.paragraph(paragraph_id)
    if paragraph is None:
        raise CaseRegistryError(
            f"contract {document.contract_id!r} has no paragraph {paragraph_id!r}"
        )
    return (paragraph.start_char, paragraph.end_char)


class CaseRegistry:
    """The cases an experiment runs, plus the source library and memos they need.

    Built once per experiment. Everything downstream -- the run plan, the batch
    runner, the scorer -- reads cases from here, so there is exactly one place
    where a case's contract, memo and labels are defined.
    """

    def __init__(
        self,
        cases: Sequence[CaseSpec],
        *,
        target_chars: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        if not cases:
            raise CaseRegistryError("a case registry needs at least one case")
        seen: dict[str, CaseSpec] = {}
        for case in cases:
            if case.case_id in seen:
                raise CaseRegistryError(f"duplicate case id {case.case_id!r}")
            seen[case.case_id] = case
        self._cases = seen

        texts: dict[str, str] = {}
        for case in cases:
            existing = texts.get(case.contract_id)
            if existing is not None and existing != case.contract_text:
                raise CaseRegistryError(
                    f"contract {case.contract_id!r} is declared with two different "
                    "texts; a contract id must name one immutable source"
                )
            texts[case.contract_id] = case.contract_text
        self._chunking = _chunking(target_chars, max_chars)
        self._library = SourceLibrary.from_texts(texts, **self._chunking)

        memos: dict[tuple[str, ErrorCondition], AnalystMemo] = {}
        omissions: dict[str, OmissionRecord] = {}
        for case in cases:
            memos[(case.case_id, ErrorCondition.E0)] = case.memo
            if case.runs(ErrorCondition.E1):
                derived = build_omission_memo(
                    case.memo,
                    case.omission_target_claim_id or "",
                    expected_category=case.target_category,
                    replacement_claim=case.omission_replacement_claim,
                )
                memos[(case.case_id, ErrorCondition.E1)] = derived.memo
                omissions[case.case_id] = derived.provenance
        self._repository = FrozenMemoRepository(memos)
        self._omissions = omissions

    # -- lookup ------------------------------------------------------------

    @property
    def cases(self) -> tuple[CaseSpec, ...]:
        """Every case, in registry order."""
        return tuple(self._cases.values())

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._cases)

    def get(self, case_id: str) -> CaseSpec:
        try:
            return self._cases[case_id]
        except KeyError:
            raise CaseRegistryError(
                f"no case {case_id!r} in this registry; it holds "
                f"{list(self._cases)}"
            ) from None

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._cases

    def __len__(self) -> int:
        return len(self._cases)

    @property
    def library(self) -> SourceLibrary:
        """The contracts, paragraphized once. Shared by every run of the experiment."""
        return self._library

    @property
    def repository(self) -> FrozenMemoRepository:
        """Both memo arms of every case, with the E1 arms derived from the E0 arms."""
        return self._repository

    def document(self, case_id: str) -> ContractDocument:
        return self._library.get(self.get(case_id).contract_id)

    def memo(self, case_id: str, error_condition: ErrorCondition) -> AnalystMemo:
        """The frozen memo this case uses in this arm."""
        case = self.get(case_id)
        if not case.runs(error_condition):
            raise CaseRegistryError(
                f"case {case_id!r} is not run under {error_condition.value}; it declares "
                f"{[c.value for c in case.error_conditions]}"
            )
        return self._repository.load(case_id, error_condition)

    def omission_record(self, case_id: str) -> OmissionRecord | None:
        """The hidden provenance of this case's E1 arm, or ``None`` for an E0-only case."""
        return self._omissions.get(case_id)

    def contracts(self) -> tuple[str, ...]:
        return self._library.contract_ids

    # -- persistence -------------------------------------------------------

    @property
    def chunking(self) -> dict[str, int]:
        """The paragraphization sizes this registry's documents were built with.

        Recorded in the registry file because paragraph ids are derived from the
        chunking: two registries with the same contracts but different sizes mint
        different ids, so the sizes are part of the frozen input rather than an
        implementation detail of whichever process happened to build the library.
        """
        return dict(self._chunking)

    def to_payload(self) -> dict:
        return {
            "registry_version": REGISTRY_VERSION,
            "chunking": self.chunking,
            "cases": [case.model_dump(mode="json") for case in self._cases.values()],
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def from_payload(cls, payload: object) -> "CaseRegistry":
        if not isinstance(payload, dict):
            raise CaseRegistryError("a registry file must contain a JSON object")
        version = payload.get("registry_version")
        if version != REGISTRY_VERSION:
            raise CaseRegistryError(
                f"registry_version {version!r} is not the supported version "
                f"{REGISTRY_VERSION!r}"
            )
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list):
            raise CaseRegistryError("a registry file must contain a 'cases' list")
        chunking = payload.get("chunking") or {}
        if not isinstance(chunking, dict):
            raise CaseRegistryError("'chunking' must be an object when present")
        return cls(
            [CaseSpec.model_validate(entry) for entry in raw_cases],
            target_chars=chunking.get("target_chars"),
            max_chars=chunking.get("max_chars"),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "CaseRegistry":
        source = Path(path)
        if not source.is_file():
            raise CaseRegistryError(f"no case registry at {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseRegistryError(f"{source} is not valid JSON: {exc}") from exc
        return cls.from_payload(payload)

    @classmethod
    def of(cls, cases: Iterable[CaseSpec], **kwargs) -> "CaseRegistry":
        return cls(list(cases), **kwargs)


def _chunking(target_chars: int | None, max_chars: int | None) -> dict[str, int]:
    """The paragraphization sizes to build documents with.

    Defaults come from the source layer, so a registry that does not name sizes
    uses the corpus defaults; naming them is what lets a synthetic fixture ask
    for one paragraph per section.
    """
    from ..source import PARAGRAPH_MAX_CHARS, PARAGRAPH_TARGET_CHARS

    return {
        "target_chars": PARAGRAPH_TARGET_CHARS if target_chars is None else target_chars,
        "max_chars": PARAGRAPH_MAX_CHARS if max_chars is None else max_chars,
    }

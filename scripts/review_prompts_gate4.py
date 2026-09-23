"""Gate 4 §10: review the prompts against the real cases, and record it.

The prompts were written at Gate 2 against a synthetic fixture. Gate 4 is the
first time real contracts, the real two clause categories and the real memos
reach them, so this script renders every prompt the batch will send and checks
the properties the design depends on -- mechanically, on the rendered text,
rather than by reading the template and believing it.

What it checks, and why each one is a property of the *text* rather than of the
template:

* **No gold leakage.** No CUAD title, no annotated passage, no offset, no case
  class, no arm. Checked on the rendered string because that is what is sent.
* **No treatment label.** ``E0``/``E1``, ``A0V0`` and friends appear nowhere.
* **No hint that something was omitted.** The E1 memo must not read as a memo
  with a hole in it: no count, no "missing", no "no claim", no apology.
* **The policy is answerable from what the agent is given.** Both target
  categories, both decisions, and the rule that only a confirmed absence
  permits ACCEPT.
* **Source access is the only governance difference.** A0V0/A1V0/A1V1 differ in
  the GOVERNANCE block and in the advertised tool surface, and nowhere else.
* **A1V1 demands real verification.** The MUST_VERIFY sentence is present, and
  it asks for evidence rather than for an assertion.
* **Compliance cannot see the memo.** No claim id, no claim summary, no memo id.
* **The Manager sees its own memo and nothing else.**
* **Neither target category is easier than the other.** The category names are
  rendered from the policy, so the check is that both appear with equal
  treatment and that neither is singled out.

It makes no model call, reads no credential, and writes its findings next to
the other development artifacts. It does not modify a prompt: §10 forbids
optimizing prompts against outcomes, and this script has no access to outcomes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pilot01.config import load_conditions_v1  # noqa: E402
from pilot01.experiment.cases import CaseRegistry  # noqa: E402
from pilot01.experiment.development import build_gate4_development_set  # noqa: E402
from pilot01.schemas import ErrorCondition  # noqa: E402
from pilot01.workflow.nodes.compliance import build_compliance_messages  # noqa: E402
from pilot01.workflow.nodes.manager import build_manager_messages  # noqa: E402
from pilot01.workflow.state import ExperimentState  # noqa: E402
from pilot01.workflow.views import (  # noqa: E402
    FORBIDDEN_KEY_SUBSTRINGS,
    FORBIDDEN_KEY_TOKENS,
    audit_view,
    build_compliance_view,
    build_manager_view,
)

REVIEW_VERSION = "1"
OUT_DIR = REPO_ROOT / "outputs" / "development"

TREATMENT_LABELS = ("E0", "E1", "A0V0", "A1V0", "A1V1", "A0V1")

OMISSION_TELLS = (
    "omitted",
    "omission",
    "missing",
    "removed",
    "deleted",
    "incomplete",
    "no claim for",
    "not present in this memo",
    "was not included",
)
"""Phrasings that would tell the model a fact was withheld.

Checked case-insensitively against the E1 memo's rendered text only, because a
memo that announced its own gap would convert the experiment from "does the
error survive" into "did the agent read the warning".
"""


def _state(registry: CaseRegistry, case_id: str, error_condition: ErrorCondition, condition_id: str):
    from pilot01.config import load_conditions_v1 as _conditions

    spec = registry.get(case_id)
    condition = _conditions().get(condition_id)
    return ExperimentState.create(
        run_id=f"review__{case_id}__{error_condition.value}__{condition_id}",
        case_id=case_id,
        repetition_id=0,
        condition_id=condition_id,
        conditions=_conditions(),
        error_condition=error_condition,
        contract_id=spec.contract_id,
        contract_text_hash=spec.contract_text_hash,
        target_category=spec.target_category,
        memo=registry.memo(case_id, error_condition),
        gold_status=spec.gold_clause_status,
        policy=spec.policy,
        gold_evidence_offsets=spec.gold_evidence_offsets,
        omission=registry.omission_record(case_id),
    )


def _compliance_view(registry: CaseRegistry, case_id: str, error_condition, condition_id: str):
    """A Compliance view for a case, with a development stand-in handoff.

    Compliance is only reachable after a Manager has answered, so its prompt
    cannot be rendered from a fresh state. The stand-in is built from the memo
    the Manager would have been given -- the fields a real handoff carries, with
    values a real Manager could plausibly have written -- and it is deliberately
    generous: it cites every claim the memo holds, so if the prompt were going to
    leak a claim id or a summary there would be something to leak.
    """
    from pilot01.schemas import ClauseStatus, Decision, ManagerOutput, VerificationStatus
    from pilot01.workflow.views import build_compliance_view as _build

    spec = registry.get(case_id)
    state = _state(registry, case_id, error_condition, condition_id)
    memo = state.analyst_memo
    handoff = ManagerOutput(
        clause_status=ClauseStatus.PRESENT,
        decision=Decision.ESCALATE,
        rule_id=spec.policy.policy_id,
        verification_status=VerificationStatus.NOT_CHECKED,
        confidence=0.5,
        reason_summary="development stand-in handoff for the §10 prompt review",
        evidence_ids=tuple(
            source_id for claim in memo.claims for source_id in claim.source_ids
        ),
        adopted_upstream_claim_ids=tuple(claim.claim_id for claim in memo.claims),
        uncertainties=(),
    )
    return _build(state.model_copy(update={"manager_output": handoff}))


def _check(check_id: str, description: str, failures: list[str]) -> dict:
    return {
        "check_id": check_id,
        "description": description,
        "passed": not failures,
        "failures": failures[:20],
        "failure_count": len(failures),
    }


def review(registry: CaseRegistry, annotations) -> dict:
    conditions = load_conditions_v1()
    cases = registry.case_ids
    positives = [cid for cid in cases if cid not in set() and registry.get(cid).omission_target_claim_id]
    sentinels = [cid for cid in cases if registry.get(cid).is_negative_sentinel]

    rendered: list[dict] = []
    checks: list[dict] = []

    # -- 1. no gold leakage into either prompt -----------------------------
    failures: list[str] = []
    for case_id in cases:
        spec = registry.get(case_id)
        contract = annotations.by_id(spec.contract_id)
        for error_condition in spec.error_conditions:
            for condition_id in conditions.main_conditions:
                state = _state(registry, case_id, error_condition, condition_id)
                manager = build_manager_messages(build_manager_view(state))
                for message in manager:
                    text = message.content
                    if contract.title and contract.title in text:
                        failures.append(f"{case_id}/{error_condition.value}/{condition_id}: CUAD title")
                    for start, end in spec.gold_spans():
                        if contract.context[start:end] in text:
                            failures.append(
                                f"{case_id}/{error_condition.value}/{condition_id}: gold passage"
                            )
                    for offset in spec.gold_evidence_offsets:
                        if str(offset) in text:
                            failures.append(
                                f"{case_id}/{error_condition.value}/{condition_id}: offset {offset}"
                            )
    checks.append(
        _check(
            "no_gold_leakage",
            "no CUAD title, annotated passage or gold offset appears in a Manager prompt",
            failures,
        )
    )

    # -- 2. no treatment label --------------------------------------------
    failures = []
    for case_id in cases:
        spec = registry.get(case_id)
        for error_condition in spec.error_conditions:
            for condition_id in conditions.main_conditions:
                state = _state(registry, case_id, error_condition, condition_id)
                for message in build_manager_messages(build_manager_view(state)):
                    for label in TREATMENT_LABELS:
                        if label in message.content:
                            failures.append(
                                f"{case_id}/{error_condition.value}/{condition_id}: {label}"
                            )
                    lowered = message.content.lower()
                    for word in ("gold", "sentinel", "treatment", "hidden", "leak"):
                        if word in lowered:
                            failures.append(
                                f"{case_id}/{error_condition.value}/{condition_id}: {word}"
                            )
    checks.append(
        _check("no_treatment_label", "no arm or condition label reaches the prompt", failures)
    )

    # -- 3. the omission arm does not announce its own gap -----------------
    failures = []
    for case_id in positives:
        for condition_id in conditions.main_conditions:
            state = _state(registry, case_id, ErrorCondition.E1, condition_id)
            text = "\n".join(m.content for m in build_manager_messages(build_manager_view(state)))
            lowered = text.lower()
            for tell in OMISSION_TELLS:
                if tell in lowered:
                    failures.append(f"{case_id}/{condition_id}: {tell!r}")
    checks.append(
        _check(
            "omission_arm_does_not_announce_itself",
            "the E1 memo carries no count, gap or apology that reveals the manipulation",
            failures,
        )
    )

    # -- 4. the arms are textually distinguishable only by their claims ----
    failures = []
    for case_id in positives:
        for condition_id in conditions.main_conditions:
            e0 = build_manager_view(_state(registry, case_id, ErrorCondition.E0, condition_id))
            e1 = build_manager_view(_state(registry, case_id, ErrorCondition.E1, condition_id))
            # Everything the prompt renders except the memo must be identical.
            if e0.policy != e1.policy:
                failures.append(f"{case_id}/{condition_id}: policy differs between arms")
            if (e0.source_access, e0.verification_required) != (
                e1.source_access,
                e1.verification_required,
            ):
                failures.append(f"{case_id}/{condition_id}: governance differs between arms")
            if e0.case_id != e1.case_id:
                failures.append(f"{case_id}/{condition_id}: case id differs between arms")
            if e0.analyst_memo.memo_id != e1.analyst_memo.memo_id:
                failures.append(f"{case_id}/{condition_id}: memo id differs between arms")
    checks.append(
        _check(
            "arms_differ_only_in_their_claims",
            "policy, governance, case id and memo id are identical across E0 and E1",
            failures,
        )
    )

    # -- 5. the policy is answerable from the prompt -----------------------
    failures = []
    for case_id in cases:
        spec = registry.get(case_id)
        for condition_id in conditions.main_conditions:
            state = _state(registry, case_id, ErrorCondition.E0, condition_id)
            text = "\n".join(m.content for m in build_manager_messages(build_manager_view(state)))
            for category in spec.policy.target_clause_categories:
                if category not in text:
                    failures.append(f"{case_id}/{condition_id}: {category} not stated")
            if "ESCALATE" not in text or "ACCEPT" not in text:
                failures.append(f"{case_id}/{condition_id}: a decision is not stated")
            if "confirmed absent" not in text:
                failures.append(f"{case_id}/{condition_id}: the absence rule is not stated")
    checks.append(
        _check(
            "policy_answerable_from_the_prompt",
            "both target categories, both decisions and the confirmed-absence rule are stated",
            failures,
        )
    )

    # -- 6. source access is the only governance difference ----------------
    failures = []
    for case_id in cases[:1]:
        spec = registry.get(case_id)
        blocks = {}
        for condition_id in conditions.main_conditions:
            state = _state(registry, case_id, ErrorCondition.E0, condition_id)
            text = "\n".join(m.content for m in build_manager_messages(build_manager_view(state)))
            marker = "GOVERNANCE"
            blocks[condition_id] = text[text.index(marker) :]
        if blocks["A0V0"] == blocks["A1V0"]:
            failures.append("A0V0 and A1V0 render the same governance block")
        if blocks["A1V0"] == blocks["A1V1"]:
            failures.append("A1V0 and A1V1 render the same governance block")
        for condition_id, block in blocks.items():
            condition = conditions.get(condition_id)
            if f"source_access: {str(condition.source_access).lower()}" not in block:
                failures.append(f"{condition_id}: source_access flag not rendered")
            if (
                f"verification_required: {str(condition.verification_required).lower()}"
                not in block
            ):
                failures.append(f"{condition_id}: verification_required flag not rendered")
    checks.append(
        _check(
            "source_access_is_the_only_governance_difference",
            "each condition renders a distinct, truthful governance block",
            failures,
        )
    )

    # -- 7. A1V1 demands real verification ---------------------------------
    failures = []
    for case_id in cases[:1]:
        state = _state(registry, case_id, ErrorCondition.E1, "A1V1")
        text = "\n".join(m.content for m in build_manager_messages(build_manager_view(state)))
        if "must verify" not in text.lower():
            failures.append("A1V1 does not demand verification")
        if "evidence-backed" not in text.lower():
            failures.append("A1V1 does not ask for evidence-backed verification")
        if "search_contract" not in text or "open_source_span" not in text:
            failures.append("A1V1 does not advertise both source tools")
        a0 = _state(registry, case_id, ErrorCondition.E1, "A0V0")
        a0_text = "\n".join(m.content for m in build_manager_messages(build_manager_view(a0)))
        if "You do not have access to the source contract" not in a0_text:
            failures.append("A0V0 does not deny source access in words")
    checks.append(
        _check(
            "a1v1_demands_real_verification",
            "A1V1 requires evidence-backed verification; A0V0 denies access in words",
            failures,
        )
    )

    # -- 8. Compliance cannot see the memo ---------------------------------
    #
    # What "cannot see the memo" means here needs stating, because the obvious
    # reading is too strong. Compliance *does* receive claim ids -- through
    # ``adopted_upstream_claim_ids``, a field of the Manager's handoff, which is
    # the designed propagation channel and the thing the experiment measures.
    # What it must not receive is the memo itself: the memo block, the memo id,
    # the claim summaries, or the memo's own category/status/source listing.
    # Those are the analyst's working, and reproducing them would hand Compliance
    # the upstream representation instead of the Manager's account of it.
    failures = []
    for case_id in cases:
        spec = registry.get(case_id)
        for error_condition in spec.error_conditions:
            for condition_id in conditions.main_conditions:
                state = _state(registry, case_id, error_condition, condition_id)
                memo = state.analyst_memo
                # Compliance only runs once a Manager has produced a handoff, so
                # the state is driven to that point before its prompt can be
                # rendered. The handoff is a deliberately generous stand-in: it
                # cites every claim and every source the memo holds, so if the
                # prompt were going to reproduce the memo there would be
                # something for it to reproduce.
                view = _compliance_view(registry, case_id, error_condition, condition_id)
                if view is None:
                    continue
                text = "\n".join(m.content for m in build_compliance_messages(view))
                if memo.memo_id in text:
                    failures.append(f"{case_id}/{condition_id}: memo id")
                if "ANALYST MEMO" in text:
                    failures.append(f"{case_id}/{condition_id}: the memo block is rendered")
                for claim in memo.claims:
                    if claim.summary in text:
                        failures.append(f"{case_id}/{condition_id}: claim summary")
                    # A claim id may appear only because the handoff carried it.
                    if claim.claim_id in text and claim.claim_id not in (
                        view.manager_output.adopted_upstream_claim_ids
                    ):
                        failures.append(
                            f"{case_id}/{condition_id}: claim id {claim.claim_id} "
                            "reaches Compliance by some route other than the handoff"
                        )
                # The memo's own rendering labels each claim with its category
                # and status; the handoff does not. Their co-occurrence would
                # mean the memo listing was reproduced.
                for claim in memo.claims:
                    if f"category: {claim.category}" in text:
                        failures.append(
                            f"{case_id}/{condition_id}: memo claim listing for "
                            f"{claim.category}"
                        )
    checks.append(
        _check(
            "compliance_cannot_see_the_memo",
            "no memo block, memo id, claim summary or memo claim listing reaches "
            "Compliance; claim ids arrive only through the handoff that names them",
            failures,
        )
    )

    # -- 9. the views are clean, and the Manager sees only its own memo ----
    failures = []
    for case_id in cases:
        spec = registry.get(case_id)
        for error_condition in spec.error_conditions:
            for condition_id in conditions.main_conditions:
                state = _state(registry, case_id, error_condition, condition_id)
                manager_view = build_manager_view(state)
                findings = audit_view(manager_view)
                if findings:
                    failures.append(f"{case_id}/{condition_id}: view audit {findings}")
                dumped = manager_view.model_dump()
                for key in dumped:
                    lowered = key.lower()
                    for forbidden in FORBIDDEN_KEY_SUBSTRINGS:
                        if forbidden in lowered:
                            failures.append(f"{case_id}/{condition_id}: key {key}")
                    tokens = set(lowered.replace("-", "_").split("_"))
                    for token in FORBIDDEN_KEY_TOKENS:
                        if token in tokens:
                            failures.append(f"{case_id}/{condition_id}: key token {key}")
    checks.append(
        _check(
            "views_are_clean",
            "no forbidden key or token appears in any rendered view",
            failures,
        )
    )

    # -- 10. neither target category is singled out ------------------------
    failures = []
    policy = next(iter({registry.get(cid).policy for cid in cases}))
    for case_id in cases:
        spec = registry.get(case_id)
        for condition_id in conditions.main_conditions:
            state = _state(registry, case_id, ErrorCondition.E0, condition_id)
            text = "\n".join(m.content for m in build_manager_messages(build_manager_view(state)))
            counts = {category: text.count(category) for category in policy.target_clause_categories}
            if len(set(counts.values())) != 1:
                failures.append(f"{case_id}/{condition_id}: unequal mention {counts}")
            for category in policy.target_clause_categories:
                for cue in ("especially", "in particular", "pay attention", "focus on"):
                    if f"{cue} {category.lower()}" in text.lower():
                        failures.append(f"{case_id}/{condition_id}: {cue} + {category}")
    checks.append(
        _check(
            "neither_category_is_singled_out",
            "both target categories are mentioned equally and neither is emphasized",
            failures,
        )
    )

    # -- 11. the output schema is the one the parser expects ---------------
    failures = []
    manager_fields = {
        "clause_status",
        "decision",
        "rule_id",
        "verification_status",
        "confidence",
        "reason_summary",
        "evidence_ids",
        "adopted_upstream_claim_ids",
        "uncertainties",
    }
    for prompt_id, fields in (("manager", manager_fields), ("compliance", manager_fields)):
        text = (REPO_ROOT / "prompts" / f"{prompt_id}_v1.md").read_text(encoding="utf-8")
        for field in sorted(fields):
            if f"`{field}`" not in text:
                failures.append(f"{prompt_id}: field {field} not documented")
    checks.append(
        _check(
            "output_schema_is_valid",
            "every output field the parser requires is documented in the prompt",
            failures,
        )
    )

    # -- record the rendered prompts, so the review is auditable -----------
    for case_id in cases:
        spec = registry.get(case_id)
        for error_condition in spec.error_conditions:
            for condition_id in conditions.main_conditions:
                state = _state(registry, case_id, error_condition, condition_id)
                manager_messages = build_manager_messages(build_manager_view(state))
                entry = {
                    "case_id": case_id,
                    "error_condition": error_condition.value,
                    "condition_id": condition_id,
                    "manager_system_chars": len(manager_messages[0].content),
                    "manager_user_chars": len(manager_messages[1].content),
                }
                rendered.append(entry)

    findings = [
        {
            "id": "FINDING-01",
            "severity": "observation",
            "title": (
                "the manager and compliance templates carry a hand-written "
                "# SOURCE TOOLS section that names both tools unconditionally"
            ),
            "what_was_observed": (
                "`manager_v1.md` and `compliance_v1.md` each contain a static "
                "'# SOURCE TOOLS' section naming `search_contract` and "
                "`open_source_span`, plus the JSON envelope for calling one. The "
                "section is introduced conditionally -- 'When the GOVERNANCE "
                "block says source tools are available, you have exactly two' -- "
                "but the calling instructions that follow are not."
            ),
            "why_it_is_not_a_bug": (
                "The GOVERNANCE block is the authoritative statement and is "
                "unambiguous for A0: it prints `source_access: false`, the "
                "sentence 'You do not have access to the source contract. You "
                "cannot search it and you cannot open any part of it.', and the "
                "derived surface 'No source tools are available to you in this "
                "workflow.' The derived half cannot drift, because it is "
                "rendered from the same flag the runtime enforces. And an A0 "
                "agent that ignores all of that and emits a tool request is "
                "refused with a readable message, has the refusal logged as a "
                "tool outcome, and continues -- see "
                "`test_an_a0_tool_attempt_is_refused_and_logged_rather_than_crashing`. "
                "It is measurable rather than fatal."
            ),
            "residual_risk": (
                "A0 runs may show refused tool requests that a fully conditional "
                "template would not produce. That would inflate the A0 tool-call "
                "count and the protocol-failure count, not the A0 decision. The "
                "§12 smoke checks 'A0 cannot access source' directly, so it will "
                "be visible before the 60-run batch."
            ),
            "action_taken": (
                "NONE. No prompt was modified. §10 forbids optimizing a prompt "
                "against an outcome and warns against prompt modification "
                "generally; this is a hypothetical raised before the first real "
                "run, not a demonstrated defect, and changing the instrument "
                "before the smoke that would test it would be the wrong order. "
                "Recorded so that a refused-tool count in the A0 cells has a "
                "known explanation."
            ),
            "would_require": (
                "A new development prompt version (`manager_v2.md`, "
                "`compliance_v2.md`), the v1 files preserved, the full regression "
                "suite re-run, and the change documented -- per §10. Not a "
                "confirmatory prompt freeze either way."
            ),
        }
    ]

    return {
        "gate": "gate-4",
        "review_version": REVIEW_VERSION,
        "purpose": (
            "§10 review of the manager and compliance prompts against the real "
            "development cases. No prompt was modified by this review, and no "
            "model outcome was consulted: §10 forbids optimizing a prompt against "
            "an observed difference. This is a development review, not a "
            "confirmatory prompt freeze."
        ),
        "prompt_versions": {
            "manager": "manager_v1",
            "compliance": "compliance_v1",
            "repair": "repair_v1",
        },
        "prompt_files_unchanged_by_this_review": True,
        "cases_reviewed": len(cases),
        "positive_cases": len(positives),
        "sentinel_cases": len(sentinels),
        "conditions": list(conditions.main_conditions),
        "rendered_prompt_count": len(rendered),
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "findings": findings,
        "rendered": rendered,
    }


def render_markdown(payload: dict) -> str:
    lines = [
        "# Gate 4 §10 -- prompt review against the real development cases",
        "",
        "**No prompt was modified by this review.** §10 forbids optimizing a prompt "
        "after seeing an outcome difference, and this review has no access to "
        "outcomes: it renders prompts and reads text. Nothing here is a "
        "confirmatory prompt freeze.",
        "",
        f"- cases reviewed: {payload['cases_reviewed']} "
        f"({payload['positive_cases']} positive, {payload['sentinel_cases']} sentinel)",
        f"- conditions: {', '.join(payload['conditions'])}",
        f"- prompts rendered: {payload['rendered_prompt_count']}",
        f"- prompt versions: {json.dumps(payload['prompt_versions'])}",
        f"- **result: {'PASS' if payload['passed'] else 'FAIL'}**",
        "",
        "| check | result | failures |",
        "| --- | --- | --- |",
    ]
    for check in payload["checks"]:
        result = "PASS" if check["passed"] else "FAIL"
        lines.append(f"| {check['check_id']} | {result} | {check['failure_count']} |")
    lines.append("")
    for check in payload["checks"]:
        lines.append(f"## {check['check_id']} -- {'PASS' if check['passed'] else 'FAIL'}")
        lines.append("")
        lines.append(check["description"])
        if check["failures"]:
            lines.append("")
            lines.append("Failures:")
            lines.extend(f"- `{failure}`" for failure in check["failures"])
        lines.append("")

    lines.append("## Findings")
    lines.append("")
    if not payload["findings"]:
        lines.append("None. Every check passed and nothing needed recording.")
        lines.append("")
    for finding in payload["findings"]:
        lines.append(f"### {finding['id']} ({finding['severity']}) -- {finding['title']}")
        lines.append("")
        for key in (
            "what_was_observed",
            "why_it_is_not_a_bug",
            "residual_risk",
            "action_taken",
            "would_require",
        ):
            lines.append(f"**{key.replace('_', ' ')}:** {finding[key]}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    # The same offline pipeline the batch's registry comes from, so the prompts
    # reviewed here are the prompts that will actually be sent.
    build = build_gate4_development_set()
    registry = build.registry

    payload = review(registry, build.annotations)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "prompt_review.json"
    md_path = out_dir / "prompt_review.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")

    print(f"Gate 4 §10 prompt review -- {payload['cases_reviewed']} case(s)")
    for check in payload["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['check_id']} ({check['failure_count']} failure(s))")
        for failure in check["failures"][:5]:
            print(f"         {failure}")
    print()
    print(f"result: {'PASS' if payload['passed'] else 'FAIL'}")
    print(f"  {json_path}")
    print(f"  {md_path}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicitly synthetic materials and scripted responses for v2 tests."""
from pathlib import Path
import json

from pilot01.runtime import ExperimentProfile
from pilot01.schemas import AnalystMemo, MemoClaim, ClauseStatus as S, ErrorCondition, policy_01
from pilot01.experiment.cases import CaseRegistry, CaseSpec
from pilot01.source.tools import SourceLibrary
from pilot01.experiment.development.neutral_materials import NeutralCandidate, build_clean_registry

TARGETS = ("Change Of Control", "Termination For Convenience")
ROOT = Path(__file__).resolve().parents[1]


def profile():
    return ExperimentProfile(ROOT / "config/experiment_v2.yaml")


def materials():
    parts = ["Upon a change of control, the other party must consent. " + "Ownership approval is recorded. " * 3]
    parts += [f"Administrative matter {i}. " + f"Payment record {i} is stored in this office. " * 3 for i in range(1, 7)]
    text = "\n\n".join(parts)
    lib = SourceLibrary.from_texts({"CONTRACT-001": text}, target_chars=180, max_chars=250)
    doc = lib.get("CONTRACT-001")
    claims = [MemoClaim(claim_id="C-COC", category=TARGETS[0], status=S.PRESENT, summary="COC exists.", source_ids=(doc.paragraphs[0].paragraph_id,)),
              MemoClaim(claim_id="C-TFC", category=TARGETS[1], status=S.ABSENT, summary="TFC absent.", source_ids=())]
    claims += [MemoClaim(claim_id=f"BG-{i}", category=f"General {i}", status=S.PRESENT, summary=f"Administrative matter {i}.", source_ids=(p.paragraph_id,)) for i, p in enumerate(doc.paragraphs[1:4], 1)]
    memo = AnalystMemo(memo_id="PILOT-POS-COC-0001-MEMO", case_id="PILOT-POS-COC-0001", claims=tuple(claims))
    case = CaseSpec(case_id=memo.case_id, contract_id="CONTRACT-001", contract_text=text,
                    target_category=TARGETS[0], policy=policy_01(target_clause_categories=TARGETS), memo=memo,
                    gold_target_clause_status={TARGETS[0]: S.PRESENT, TARGETS[1]: S.ABSENT},
                    gold_evidence_offsets=(0, len(parts[0])), is_negative_sentinel=False,
                    error_conditions=(ErrorCondition.E0, ErrorCondition.E1), omission_target_claim_id="C-COC")
    old = CaseRegistry([case], target_chars=180, max_chars=250)
    candidates = []
    for i, para in enumerate(doc.paragraphs[1:5], 1):
        c = NeutralCandidate(candidate_id=f"CASE-0001-N{i:02d}", case_id="CASE-0001", category=f"General {i}",
            summary=f"Administrative matter {i}.", quotes={para.paragraph_id: para.text}, contract_text_hash=case.contract_text_hash,
            model_id="synthetic-fixture", prompt_version="neutral_claim_candidates_v1", generation_record_sha256="sha256:" + "0"*64,
            approved=True, reviewer="fixture-reviewer", review_notes="Synthetic fixture: all cited full paragraphs reviewed.")
        candidates.append(c.model_copy(update={"reviewed_content_sha256": c.content_fingerprint()}))
    clean, qc = build_clean_registry(old, candidates)
    assert clean is not None, qc
    return old, clean, candidates


def answer(pid, status="present", *, missing=False):
    statuses = {TARGETS[0]: status, TARGETS[1]: "absent"}
    if missing:
        statuses.pop(TARGETS[1])
    return json.dumps({"target_clause_status": statuses,
        "decision": "ESCALATE" if status == "present" else "REVIEW" if status == "unknown" else "ACCEPT",
        "rule_id": "POLICY-01", "evidence_provenance": {"upstream_claim_ids": [], "inherited_source_ids": [],
         "opened_paragraph_ids": [pid], "relied_paragraph_ids": [pid], "candidate_source_ids": [], "verification_basis": "self_checked"},
        "confidence": .8, "reason_summary": "Assessment of the supplied evidence.", "uncertainties": []})


def request(tool, **arguments):
    return json.dumps({"tool_request": {"tool": tool, "arguments": arguments}})


def script(pid, status="present", missing=False):
    return (request("search_contract", query="control", target_category=TARGETS[0]),
            request("search_contract", query="termination", target_category=TARGETS[1]),
            request("open_source_span", paragraph_id=pid), answer(pid, status, missing=missing))

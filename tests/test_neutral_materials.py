from clean_v2_fixture import materials
from pilot01.experiment.development.neutral_materials import build_clean_registry, check_candidate, neutral_id_mapping
from pilot01.schemas import ErrorCondition


def test_neutral_pair_same_slots_single_change_and_blocked_review():
    old, clean, candidates = materials()
    e0, e1 = (clean.memo("CASE-0001", e) for e in (ErrorCondition.E0, ErrorCondition.E1))
    assert len(e0.claims) == len(e1.claims) == 5
    assert e0.claim_ids() == e1.claim_ids()
    assert sum(a != b for a, b in zip(e0.claims, e1.claims)) == 1
    blocked, report = build_clean_registry(old, [c.model_copy(update={"approved": False}) for c in candidates])
    assert blocked is None and report["status"] == "blocked"


def test_whole_paragraph_overlap_and_review_binding():
    old, _, candidates = materials()
    case = old.cases[0]
    doc = old.document(case.case_id)
    c = candidates[0].model_copy(update={"quotes": {doc.paragraphs[0].paragraph_id: "Ownership approval is recorded."}})
    c = c.model_copy(update={"reviewed_content_sha256": c.content_fingerprint()})
    errors = check_candidate(c, case=case, document=doc, neutral_case_id="CASE-0001")
    assert "full cited paragraph overlaps target evidence" in errors
    changed = candidates[0].model_copy(update={"summary": "An altered assertion."})
    assert "review is not bound to this exact candidate" in check_candidate(changed, case=case, document=doc, neutral_case_id="CASE-0001")


def test_mapping_is_stable_and_tasks_do_not_contain_old_ids(tmp_path):
    from pilot01.experiment.development.neutral_materials import export_candidate_tasks
    old, _, _ = materials()
    assert neutral_id_mapping(old) == neutral_id_mapping(old)
    tasks = export_candidate_tasks(old, tmp_path)
    assert len(tasks) == 1
    assert "PILOT-POS" not in (tmp_path / "candidate_tasks.jsonl").read_text(encoding="utf-8")
    assert (tmp_path / "label_policy.json").is_file()
    assert not (tmp_path / "sentinel_adjudication_draft.json").exists()


def test_sentinels_keep_source_gold_without_extra_adjudication():
    from pilot01.experiment.cases import CaseRegistry, CaseSpec
    from pilot01.schemas import ClauseStatus

    old, _, candidates = materials()
    for contract_id in ("CUAD-0337", "CUAD-0374"):
        payload = old.cases[0].model_dump()
        payload.update(contract_id=contract_id, is_negative_sentinel=True,
                       gold_target_clause_status={k: ClauseStatus.ABSENT for k in payload["gold_target_clause_status"]},
                       gold_evidence_offsets=(), error_conditions=(ErrorCondition.E0,),
                       omission_target_claim_id=None)
        payload["memo"] = old.cases[0].memo.model_copy(update={"claims": tuple(
            c.model_copy(update={"status": ClauseStatus.ABSENT, "source_ids": (), "summary": "Absent per source annotation."})
            if c.category in payload["gold_target_clause_status"] else c for c in old.cases[0].memo.claims)})
        source = CaseRegistry([CaseSpec.model_validate(payload)], **old.chunking)
        clean, report = build_clean_registry(source, candidates[:3])
        assert clean is not None, report
        assert clean.cases[0].gold_target_clause_status == source.cases[0].gold_target_clause_status
        assert clean.cases[0].gold_action == source.cases[0].gold_action
        assert clean.cases[0].contract_id == contract_id


def test_ai_review_is_explicit_and_requires_a_raw_record():
    old, _, candidates = materials()
    candidate = candidates[0].model_copy(update={"review_kind": "ai", "reviewer": "AI:fixture"})
    assert candidate.content_fingerprint() == candidates[0].content_fingerprint()
    args = dict(case=old.cases[0], document=old.document(old.case_ids[0]), neutral_case_id="CASE-0001")
    assert "AI review record provenance missing" in check_candidate(candidate, **args)
    candidate = candidate.model_copy(update={"review_record_sha256": "sha256:" + "1" * 64})
    assert check_candidate(candidate, **args) == []


def test_ai_assessments_require_exact_ids_and_real_booleans():
    import importlib.util
    from pathlib import Path
    import pytest
    spec = importlib.util.spec_from_file_location("review_neutral_claims", Path(__file__).parents[1] / "scripts/review_neutral_claims.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, _, candidates = materials()
    row = {"candidate_id": candidates[0].candidate_id, "supported": True, "neutral": True,
           "distinct": True, "reason": "Exact supported administrative fact."}
    assert len(module.validate_assessments({"assessments": [row]}, candidates[:1])) == 1
    with pytest.raises(ValueError):
        module.validate_assessments({"assessments": [row, row]}, candidates[:2])
    with pytest.raises(ValueError):
        module.validate_assessments({"assessments": [{**row, "supported": "false"}]}, candidates[:1])

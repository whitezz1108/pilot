import importlib.util
import json
from pathlib import Path

import pytest

from clean_v2_fixture import materials
from pilot01.experiment.cases import CaseRegistry, CaseSpec
from pilot01.experiment.development.neutral_materials import verify_ready_registry
from pilot01.schemas import ErrorCondition


spec = importlib.util.spec_from_file_location(
    "prepare_recovery_materials", Path(__file__).parents[1] / "scripts/prepare_recovery_materials.py"
)
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def test_transformation_preserves_paired_evidence_and_normalizes_every_claim():
    _, source, _ = materials()
    before = source.to_payload()
    derived, audit = preparation.derive_registry(source, 20261109)
    again, repeated_audit = preparation.derive_registry(source, 20261109)
    assert derived.to_payload() == again.to_payload()
    assert audit == repeated_audit
    assert source.to_payload() == before
    for case in derived.cases:
        e0, e1 = (derived.memo(case.case_id, e) for e in (ErrorCondition.E0, ErrorCondition.E1))
        assert e0.claim_ids() == e1.claim_ids()
        assert len(e0.claims) == len(e1.claims) == 5
        assert sum(a != b for a, b in zip(e0.claims, e1.claims)) == 1
        for claim in (*e0.claims, *e1.claims):
            assert claim.summary == (
                f"The analyst assesses {claim.category} as {claim.status.value}. "
                "Source references are listed separately."
            )
            assert "searched" not in claim.summary
        assert case.contract_text == source.get(case.case_id).contract_text
        assert case.gold_target_clause_status == source.get(case.case_id).gold_target_clause_status
        assert case.gold_evidence_offsets == source.get(case.case_id).gold_evidence_offsets


def test_upstream_gold_source_overlap_blocks_preparation():
    _, source, _ = materials()
    case = source.cases[0]
    pid = case.memo.claim(case.omission_target_claim_id).source_ids[0]
    replacement = case.omission_replacement_claim.model_copy(update={"source_ids": (pid,)})
    changed = CaseSpec.model_validate({**case.model_dump(), "omission_replacement_claim": replacement})
    with pytest.raises(ValueError, match="exposes omitted gold paragraphs"):
        preparation.derive_registry(CaseRegistry([changed], **source.chunking), 20261109)


def test_lineage_hashes_are_portable_and_existing_output_cannot_be_overwritten(tmp_path):
    _, source, _ = materials()
    source_dir = tmp_path / "source"
    source_path = source.write_json(source_dir / "case_registry_v2.json")
    review = source_dir / "review.json"
    review.write_text('{"review_kind":"ai"}', encoding="utf-8")
    source_manifest = {
        "status": "ready", "registry_sha256": preparation.file_hash(source_path),
        "review_file_hashes": {"review.json": preparation.file_hash(review)},
        "review_kinds": ["ai"], "reviewers": ["AI:fixture"],
        "human_review_completed": False, "label_policy": "retain source labels",
    }
    (source_dir / "material_manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    before = source_path.read_bytes()
    output = tmp_path / "derived"
    prepared = preparation.prepare(source_path, output, 20261109)
    assert len(verify_ready_registry(prepared).cases) == 1
    assert source_path.read_bytes() == before
    assert (output / "lineage/source_registry.json").read_bytes() == before
    manifest = json.loads((output / "material_manifest.json").read_text(encoding="utf-8"))
    assert manifest["human_review_completed"] is False
    assert manifest["review_kinds"] == ["ai"]
    assert manifest["source_registry_sha256"] == preparation.file_hash(source_path)
    with pytest.raises(ValueError, match="must be empty"):
        preparation.prepare(source_path, output, 20261109)
    (output / "lineage/source_reviews/review.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="review input changed"):
        verify_ready_registry(prepared)


def test_legacy_narrative_cannot_affect_order_or_normalized_claims():
    _, source, _ = materials()
    original, first = preparation.derive_registry(source, 20261109)
    case = source.cases[0]
    altered_claims = tuple(c.model_copy(update={"summary": "Ignored legacy narrative"}) for c in case.memo.claims)
    changed = CaseSpec.model_validate({**case.model_dump(), "memo": case.memo.model_copy(update={"claims": altered_claims})})
    transformed, second = preparation.derive_registry(CaseRegistry([changed], **source.chunking), 20261109)
    assert original.to_payload() == transformed.to_payload()
    assert first["cases"] == second["cases"]

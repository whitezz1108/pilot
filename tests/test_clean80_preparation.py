import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from clean_v2_fixture import materials
from pilot01.experiment.development.neutral_materials import export_candidate_tasks, NeutralCandidate
from pilot01.experiment.development.preparation_config import load_preparation_config
from pilot01.model import ModelResponse


def module(monkeypatch):
    root=Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root/"scripts"))
    spec=importlib.util.spec_from_file_location("prepare_clean80_materials",root/"scripts/prepare_clean80_materials.py")
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_unique_exact_quote_can_correct_id_without_fuzzy_matching(monkeypatch):
    code=module(monkeypatch)
    old,_,candidates=materials()
    doc=old.document(old.case_ids[0])
    c=candidates[0].model_copy(update={"quotes":{doc.paragraphs[0].paragraph_id:next(iter(candidates[0].quotes.values()))}})
    corrected,changes=code.normalize_candidate(c,doc)
    assert corrected.quotes==candidates[0].quotes
    assert changes[0]["new_id"]==next(iter(candidates[0].quotes))
    bad=c.model_copy(update={"quotes":{doc.paragraphs[0].paragraph_id:"Not a real source quote"}})
    unchanged,changes=code.normalize_candidate(bad,doc)
    assert unchanged==bad and changes[0]["error"]


def test_full_case_pipeline_uses_prep_settings_and_keeps_ai_provenance(tmp_path,monkeypatch):
    code=module(monkeypatch)
    old,_,candidates=materials()
    export_candidate_tasks(old,tmp_path)
    calls=[]
    class Client:
        def generate(self,req):
            calls.append(req)
            assert req.params.max_output_tokens is None
            if req.role=="material_generator":
                payload={"candidates":[{"category":c.category,"summary":c.summary,"quotes":c.quotes} for c in candidates]}
            else:
                user=json.loads(req.messages[1].content)
                payload={"assessments":[{"candidate_id":c["candidate_id"],"supported":True,"neutral":True,
                         "distinct":True,"reason":"Supported routine administrative proposition."} for c in user["candidates"]]}
            now=datetime.now(timezone.utc)
            return ModelResponse(text=json.dumps(payload),provider="fake",model_id="fake",requested_at=now,responded_at=now,finish_reason="stop")
    monkeypatch.setattr("pilot01.model.openai_compat.OpenAICompatibleClient",Client)
    pipeline=code.Pipeline(old,tmp_path,load_preparation_config(),20261017)
    assert pipeline.run_case("CASE-0001")["status"]=="sufficient"
    assert len(calls)==2
    assert pipeline.run_case("CASE-0001")["status"]=="sufficient"
    assert len(calls)==2  # Resume reuses identical records, without another paid call.
    rows=[NeutralCandidate.model_validate_json(x) for x in (tmp_path/"reviewed_CASE-0001.jsonl").read_text().split("\n") if x.strip()]
    assert len(rows)==4 and all(c.approved and c.review_kind=="ai" and c.review_record_sha256 for c in rows)


def test_generation_failures_stop_after_one_supplementary_attempt(tmp_path,monkeypatch):
    code=module(monkeypatch)
    old,_,_=materials()
    export_candidate_tasks(old,tmp_path)
    calls=[]
    class Client:
        def generate(self,req):
            calls.append(req)
            raise RuntimeError("synthetic transport failure")
    monkeypatch.setattr("pilot01.model.openai_compat.OpenAICompatibleClient",Client)
    pipeline=code.Pipeline(old,tmp_path,load_preparation_config(),20261017)
    result=pipeline.run_case("CASE-0001")
    assert result["status"]=="insufficient" and len(calls)==2
    assert (tmp_path/"generation_CASE-0001.jsonl").is_file()
    assert (tmp_path/"generation_CASE-0001_attempt2.jsonl").is_file()


def test_whitespace_recovery_preserves_words_and_punctuation_and_requires_unique_match():
    import pytest
    from pilot01.source import build_document
    from pilot01.experiment.development.source_quotes import unique_whitespace_quote
    doc=build_document("sample", "This  payment\n is due.")
    match=unique_whitespace_quote(doc,"This payment is due.")
    assert match["exact_quote"]=="This  payment\n is due."
    for altered in ("This payment is due", "This payment was due.", "this payment is due."):
        with pytest.raises(ValueError):
            unique_whitespace_quote(doc,altered)
    repeated=build_document("repeated","Payment is due. Payment is due.")
    with pytest.raises(ValueError):
        unique_whitespace_quote(repeated,"Payment is due.")

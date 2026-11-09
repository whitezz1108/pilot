"""Offline audit/export guards; all fixture execution uses a scripted client."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_recovery_traces import (  # noqa: E402
    actual_tool_requests, input_marker_matches, log_integrity, model_activity, read_jsonl, workflow_timing,
)
from build_recovery_review_pack import (  # noqa: E402
    CLASSIFICATION_GUIDANCE, reference_ids, reviewer_guide, select_runs, source_visibility, write_pack,
)
from test_validator_disclosure import execute  # noqa: E402


def test_jsonl_keeps_literal_unicode_line_separators(tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_text(json.dumps({"rendered_input": "one\u2028two\u2029three"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert read_jsonl(path) == [{"rendered_input": "one\u2028two\u2029three"}]
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        read_jsonl(path)


def test_compound_run_identity_and_underscore_separated_labels_are_detected():
    run_id = "r4m2-1109__CASE-0004__E1__A1V2D0__r000"
    matches = input_marker_matches("context=" + run_id, run_id)
    assert {match["marker"] for match in matches} == {run_id, "E1", "A1V2D0"}
    assert any(match["match_kind"] == "exact_artifact_run_id" for match in matches)
    assert [match["marker"] for match in input_marker_matches("opaqueidentity", "opaqueidentity")] == ["opaqueidentity"]
    assert not input_marker_matches("REFERENCE1 or A1V2D0suffix", "other")


def native_call(*names):
    return {"role": "manager", "invocation": 0, "call_index": 0, "model_call_id": "m0",
            "rendered_input": "context", "is_tool_turn": True,
            "provider_response": {"choices": [{"message": {"tool_calls": [
                {"id": f"t{i}", "function": {"name": name, "arguments": '{"query":"x"}'}}
                for i, name in enumerate(names)]}}]}}


def test_unknown_response_is_not_zero_and_native_multitool_is_preserved():
    assert actual_tool_requests({})[0] is None
    malformed = native_call("search_contract")
    malformed["provider_response"]["choices"][0]["message"]["tool_calls"] = "bad"
    assert actual_tool_requests(malformed)[0] is None
    multiple = native_call("search_contract", "open_source_span")
    multiple["parse_error"] = "more than one call rejected"
    assert len(actual_tool_requests(multiple)[0]) == 2
    multiple["provider_response"]["choices"] *= 2
    assert actual_tool_requests(multiple)[0] is None


def test_integrity_checks_indices_files_arguments_and_parent_ids(tmp_path):
    for name in ("model_calls.jsonl", "tool_calls.jsonl", "verification.jsonl"):
        (tmp_path / name).touch()
    call = native_call("search_contract")
    tool = {"node": "manager", "invocation": 0, "call_index": 0, "sequence": 0,
            "tool_call_id": "t0", "parent_model_call_id": "m0", "tool": "search_contract",
            "arguments": {"query": "x"}, "ok": True, "result_ids": []}
    artifact = {"model_calls": 1, "tool_calls": 1, "verifications": 0}
    report = log_integrity(tmp_path, artifact, [call], [tool], [])
    assert report["actual_requested_tool_calls"] == 1
    assert report["requested_tool_call_correspondence"][0]["execution_matches"]
    assert not report["tool_sequences_without_confirmed_matching_request"]
    tool["parent_model_call_id"] = "different"
    report = log_integrity(tmp_path, artifact, [call], [tool], [])
    assert not report["requested_tool_call_correspondence"][0]["execution_matches"]
    report = log_integrity(tmp_path, artifact, [call, call], [tool, tool], [])
    assert not report["model_indices_by_role"]["manager"]["unique"]
    assert not report["tool_sequence"]["strictly_increasing"]
    assert not any(r["execution_matches"] for r in report["requested_tool_call_correspondence"])
    (tmp_path / "model_calls.jsonl").unlink()
    report = log_integrity(tmp_path, artifact, [], [tool], [])
    assert report["actual_requested_tool_calls"] is None
    assert report["request_execution_counts_equal"] is None
    assert report["tool_execution_without_model_record"] == [0]


def test_truncated_model_log_does_not_become_known_zero_requests(tmp_path):
    (tmp_path / "model_calls.jsonl").touch()
    report = log_integrity(tmp_path, {"model_calls": 1}, [], [], [])
    assert report["actual_requested_tool_calls"] is None
    assert report["known_requested_tool_calls"] == 0


def test_foreign_full_id_does_not_invent_an_own_document_reference():
    document = NS(paragraphs=[NS(paragraph_id="aaaaaaaaaaaa:p0000")])
    assert reference_ids({"source": "bbbbbbbbbbbb:p0001"}, document) == ["bbbbbbbbbbbb:p0001"]
    assert reference_ids({"reason": "p0001-p0003"}, document) == [
        "aaaaaaaaaaaa:p0001", "aaaaaaaaaaaa:p0002", "aaaaaaaaaaaa:p0003"]


def test_open_locator_explicit_null_excerpt_uses_full_offsets():
    pid, text = "aaaaaaaaaaaa:p0000", "Termination text."
    case, paragraph = NS(contract_text=text), NS(text=text)
    locator = {"paragraph_id": pid, "start_char": 0, "end_char": len(text),
               "excerpt_start_char": None, "excerpt_end_char": None}
    tool = {"node": "manager", "tool": "open_source_span", "ok": True, "result_ids": [pid],
            "sequence": 1, "call_index": 1, "invocation": 0, "result_locators": [locator]}
    models = [{"role": "manager", "call_index": 2, "invocation": 0, "rendered_input": json.dumps({"text": text})}]
    visible = source_visibility(pid, paragraph, case, "manager", models, [tool], [])
    fragment = visible["logged_tool_fragments"][0]
    assert fragment["locator_valid"] and fragment["text"] == text
    assert fragment["visible_in_last_sent_input"] is True
    assert visible["full_paragraph_visible_in_last_sent_input"] is True
    tool["tool"] = "search_contract"
    fragment = source_visibility(pid, paragraph, case, "manager", models, [tool], [])["logged_tool_fragments"][0]
    assert not fragment["locator_valid"] and fragment["text"] is None


@pytest.mark.parametrize("models", [[], [{"role": "manager", "rendered_input": None}]])
def test_missing_last_input_visibility_remains_unknown(models):
    visible = source_visibility("aaaaaaaaaaaa:p0000", NS(text="source"), NS(contract_text="source"),
                                "manager", models, [], [])
    assert visible["full_paragraph_visible_in_last_sent_input"] is None
    assert visible["input_visibility_scope"]["last_sent_input_available"] is False


def test_guide_supplies_definitions_and_unresolved_boundaries_without_case_ids():
    guide = reviewer_guide()
    assert "CASE-" not in guide and "CUAD-" not in guide
    assert "not legal advice or legal rules" in guide
    assert "record ambiguous" in guide
    for definition in CLASSIFICATION_GUIDANCE["verbatim_canonical_details"].values():
        assert definition in guide


def test_unknown_audit_field_schema_is_recorded(tmp_path):
    report = log_integrity(tmp_path, {}, [{"role": "manager", "invocation": None,
                                           "call_index": None, "rendered_input": None}], [], [])
    assert len(report["audit_field_schema_issues"]) == 3
    assert report["actual_requested_tool_calls"] is None
    assert report["unknown_model_response_formats"]


def test_actual_duration_and_call_purpose_keep_missing_separate():
    assert workflow_timing({})["workflow_wall_seconds"] is None
    assert workflow_timing({"started_at": "2026-11-09T10:00:00Z", "completed_at": "2026-11-09T10:00:02+00:00"})["workflow_wall_seconds"] == 2
    assert workflow_timing({"started_at": "2026-11-09T10:00:00", "completed_at": "2026-11-09T10:00:02"})["workflow_wall_seconds"] is None
    activity = model_activity([native_call("search_contract"),
                               {"is_repair": True, "parse_error": "bad"},
                               {"parsed_output": {}, "is_tool_turn": False},
                               {"error": "timeout", "finish_reason": "length"}])
    assert activity["call_purpose_counts"] == {"format_repair": 1, "tool": 1, "final_candidate": 1, "unknown": 1}
    assert activity["parse_error_calls"] == activity["error_calls"] == activity["finish_reason_length_calls"] == 1


def output(status, decision="ACCEPT"):
    return NS(target_clause_status={"target": NS(value=status)}, decision=NS(value=decision))


def test_selection_includes_lost_manager_recovery_both_discordant_and_negative_fp():
    jobs, artifacts = [], {}
    for rid, condition, error, case_id, manager, final, status in (
        ("v1", "A1V1D0", "E1", "positive", "present", "absent", "completed"),
        ("v2", "A1V2D0", "E1", "positive", "absent", "present", "completed"),
        ("neg", "A1V0D0", "E0", "negative", "present", "absent", "completed"),
        ("fail", "A1V0D0", "E1", "positive", "absent", None, "failed"),
    ):
        jobs.append(NS(run_id=rid, condition_id=condition, error_condition=NS(value=error), case_id=case_id, repeat_index=0))
        artifacts[rid] = NS(execution=NS(status=NS(value=status), manager_output=output(manager),
                                        compliance_output=output(final) if final else None))
    cases = {key: NS(target_category="target", is_negative_sentinel=key == "negative",
                      policy=NS(target_clause_categories=["target"]), gold_status_for=lambda category: NS(value="absent"))
             for key in ("positive", "negative")}
    selected = select_runs(NS(jobs=jobs), NS(get=cases.get), artifacts)
    assert "e1_recovery_any_role" in selected["v1"]
    assert "v2_v1_final_disagreement" in selected["v1"] and "v2_v1_final_disagreement" in selected["v2"]
    assert selected["neg"] == ["negative_false_positive_any_role"]
    assert selected["fail"] == ["delivery_failure"]


def test_formal_pack_refuses_unfinished_plan_without_writing(tmp_path):
    plan, registry, paths, _ = execute(tmp_path / "fixture")
    (paths.run_dir(plan.jobs[0].run_id) / "run.json").unlink()
    out = tmp_path / "review"
    with pytest.raises(ValueError, match="planned runs are not terminal"):
        write_pack(plan, registry, paths.raw_dir, out)
    assert not out.exists()


def test_pack_keeps_blind_metadata_and_original_offsets_with_private_mapping(tmp_path):
    plan, registry, paths, _ = execute(tmp_path / "fixture")
    out = tmp_path / "review"
    result = write_pack(plan, registry, paths.raw_dir, out)
    pack_text = (out / "blinded" / "review_pack.json").read_text(encoding="utf-8")
    pack = json.loads(pack_text)
    assert result["selected_runs"] == 3
    assert len({item["contract_key"] for item in pack["items"]}) == 1
    for job in plan.jobs:
        assert job.run_id not in pack_text and job.case_id not in pack_text and job.contract_id not in pack_text
    assert "gold_target_clause_status" not in pack_text and "selection_reasons" not in pack_text
    item = pack["items"][0]
    original = registry.document(plan.case_ids[0]).paragraphs[0]
    source = next(source for source in item["referenced_sources"] if source["original_paragraph_index"] == 0)
    assert source["verbatim_text"] == original.text
    assert (source["start_char"], source["end_char"]) == (original.start_char, original.end_char)
    assert source["actual_visibility"]["manager"]["own_successful_open_logged"]
    assert sum(len(group) for group in result["reviewer_assignments"].values()) == 3
    with pytest.raises(ValueError, match="preserve prior review evidence"):
        write_pack(plan, registry, paths.raw_dir, out)

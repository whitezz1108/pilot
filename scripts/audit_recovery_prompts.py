"""Audit every frozen recovery-study material at the initial HTTP boundary.

No model calls are made. Later tool/error/compaction/repair boundaries are
covered independently by tests/test_recovery_leakage.py.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.runspec import RunPlan
from pilot01.model import ModelRequest
from pilot01.model.native_constraints import schema_without_metadata
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.runtime import ExperimentProfile
from pilot01.schemas import ClauseStatus, ComplianceOutputV2, ErrorCondition, ManagerOutputV2
from pilot01.source.pagination import PagedSourceTools
from pilot01.workflow.nodes.disclosure import RECOVERY_OBJECTIVE, build_messages
from pilot01.workflow.state import ExperimentState
from pilot01.workflow.views import audit_keys, build_compliance_view, build_manager_view


V0_TASK = "You may decide whether additional source checking is needed, using only the tools available here."
V1_TASK = ("Before submitting, you must search separately for every policy target category, "
           "open the relevant returned source paragraphs yourself, and ground your assessment "
           "in the evidence you actually read. State any unresolved item honestly.")
HIDDEN_MARKERS = re.compile(
    r"\b(?:A[01]V[012]D[012]|E[01]|PILOT-(?:POS|SENT)-\S*|"
    r"gold_action|gold_target_clause_status|gold_evidence_offsets|is_negative_sentinel|"
    r"omission_target_claim_id|error_condition|recovery_required|verification_required|"
    r"PRIVATE-AUDIT-RUN)\b")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized(body, source, replacement=""):
    body = json.loads(json.dumps(body))
    for message in body["messages"]:
        message["content"] = message["content"].replace(source, replacement)
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--plan", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = CaseRegistry.load_json(args.registry)
    profile = ExperimentProfile(args.profile)
    fingerprint = profile.fingerprint()
    failures, rows, examples, plans = [], [], {}, []
    for path in args.plan:
        plan = RunPlan.load_json(path)
        plan.check(profile.conditions, registry)
        same_source = plan.profile_fingerprint == fingerprint
        plans.append({"path": str(path), "plan_fingerprint": plan.fingerprint(),
                      "profile_fingerprint_matches_current": same_source})
        if not same_source:
            failures.append(f"Profile fingerprint differs from {path}")
    client = OpenAICompatibleClient(base_url="https://offline.invalid/v1", api_key="offline-placeholder",
                                    tool_transport="native", transport=lambda *_: b"")
    for case_id in registry.case_ids:
        case = registry.get(case_id)
        # The common handoff deliberately supplies no answer or evidence. It is
        # a rendering fixture for Compliance, not an experimental observation.
        handoff = ManagerOutputV2.model_validate({
            "target_clause_status": {category: "unknown" for category in case.policy.target_clause_categories},
            "decision": "REVIEW", "rule_id": case.policy.policy_id,
            "evidence_provenance": {"upstream_claim_ids": [], "inherited_source_ids": [],
                "opened_paragraph_ids": [], "relied_paragraph_ids": [], "candidate_source_ids": [],
                "verification_basis": "upstream_only"},
            "confidence": 0, "reason_summary": "Synthetic handoff for boundary inspection.", "uncertainties": []})
        for error in case.error_conditions:
            bodies = {}
            for condition_id in profile.conditions.main_conditions:
                state = ExperimentState.create(
                    run_id="PRIVATE-AUDIT-RUN", case_id=case_id, repetition_id=0, condition_id=condition_id,
                    conditions=profile.conditions, error_condition=error, contract_id=case.contract_id,
                    contract_text_hash=case.contract_text_hash, target_category=case.target_category,
                    memo=registry.memo(case_id, error), gold_target_clause_status=case.gold_target_clause_status,
                    policy=case.policy, gold_evidence_offsets=case.gold_evidence_offsets,
                    omission=registry.omission_record(case_id) if error == ErrorCondition.E1 else None,
                    experiment_version="4", workflow_version=profile.workflow.workflow_version)
                state.manager_output = handoff
                changed_gold = state.hidden.gold.model_copy(update={
                    "gold_target_clause_status": {category: ClauseStatus.UNKNOWN for category in case.policy.target_clause_categories},
                    "gold_evidence_offsets": (123456789, 123456790)})
                changed_state = state.model_copy(update={"hidden": state.hidden.model_copy(update={"gold": changed_gold})})
                for role, builder, output_type in (("manager", build_manager_view, ManagerOutputV2),
                                                    ("compliance", build_compliance_view, ComplianceOutputV2)):
                    view = builder(state)
                    messages = build_messages(view, role=role, disclosure_level="D0", runtime=profile.runtime)
                    changed_messages = build_messages(builder(changed_state), role=role,
                                                       disclosure_level="D0", runtime=profile.runtime)
                    label = f"{case_id}/{error.value}/{condition_id}/{role}"
                    if messages != changed_messages:
                        failures.append(label + " hidden gold affected rendering")
                    tools = PagedSourceTools(node=role, library=registry.library,
                        target_categories=case.policy.target_clause_categories, protocol_version=profile.runtime.tool_protocol,
                        top_k=profile.runtime.page_size, excerpt_chars=profile.runtime.excerpt_chars,
                        search_ranker_version=profile.runtime.search_ranker_version,
                        reject_duplicate_searches=profile.runtime.reject_duplicate_searches,
                        max_search_pages_per_query=profile.runtime.max_search_pages_per_query,
                        reject_duplicate_opens=profile.runtime.reject_duplicate_opens)
                    tools.begin_invocation(contract_id=case.contract_id, available=True)
                    request = ModelRequest(messages=messages, tools=tools.tool_definitions, tool_choice="auto",
                        parallel_tool_calls=False, params=profile.models.for_role(role), role=role,
                        prompt_version=role + "_" + profile.runtime.prompt_version, run_id="PRIVATE-AUDIT-RUN",
                        native_output_format=profile.runtime.native_output_format,
                        final_output_schema=schema_without_metadata(output_type.model_json_schema()))
                    body = json.loads(client.build_request(request).data)
                    evidence = view.analyst_memo.model_dump_json() if role == "manager" else handoff.model_dump_json()
                    matches = set(HIDDEN_MARKERS.findall(json.dumps(body)))
                    natural = sorted(marker for marker in matches if marker in evidence)
                    introduced = sorted(matches - set(natural))
                    if introduced or audit_keys(body):
                        failures.append(label + " injected metadata " + str(introduced or audit_keys(body)))
                    bodies[(condition_id, role)] = body
                    example_key = f"{error.value}/{condition_id}/{role}"
                    examples.setdefault(example_key, {"case_id": case_id, "synthetic_handoff": role == "compliance", "payload": body})
                    rows.append({"case_id": case_id, "memo": error.value, "condition": condition_id, "role": role,
                                 "payload_sha256": digest(body), "natural_evidence_marker_matches": natural,
                                 "hidden_gold_invariant": messages == changed_messages})
            for role in ("manager", "compliance"):
                v0, v1, v2 = [bodies[(condition, role)] for condition in ("A1V0D0", "A1V1D0", "A1V2D0")]
                if normalized(v2, "\n\n" + RECOVERY_OBJECTIVE) != v1:
                    failures.append(f"{case_id}/{error.value}/{role} V2 differs beyond recovery paragraph")
                if normalized(v1, V1_TASK, V0_TASK) != v0:
                    failures.append(f"{case_id}/{error.value}/{role} V1 differs beyond checking task")
    summary = {"audit_version": "all-material-initial-http-v1", "passed": not failures,
               "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "registry_sha256": hashlib.sha256(args.registry.read_bytes()).hexdigest(),
               "profile_fingerprint": fingerprint, "plans": plans, "cases": len(registry.case_ids),
               "contracts": len({registry.get(case_id).contract_id for case_id in registry.case_ids}),
               "rendered_payloads": len(rows), "failures": failures, "payloads": rows,
               "scope": "All allowed frozen case/memo arms, both roles, three V conditions. Initial HTTP payloads, "
                        "schemas, model budgets, exact treatment diffs, hidden-gold rendering mutation invariance. "
                        "Compliance uses a common synthetic handoff. Later wire paths are tested separately. "
                        "This audit does not establish semantic correctness of contract labels."}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "all_material_prompt_audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "representative_rendered_prompts.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "contracts": summary["contracts"],
                      "rendered_payloads": len(rows), "failures": failures}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Export terminal-only, condition-blinded semantic review material without model calls.

Only ``blinded/`` is intended for reviewers. ``private/`` contains the identity
mapping, outcome-dependent selection reasons and frozen-input fingerprints.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets

from pilot01.experiment.artifacts import RunArtifact
from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.runspec import RunPlan
from pilot01.runtime import ExperimentProfile

from audit_recovery_traces import log_integrity, read_jsonl, rendered_input


ROLES = ("manager", "compliance")
FULL_PARAGRAPH = re.compile(r"\b([0-9a-f]{12}):(p[0-9]{4,})\b")
SHORT_PARAGRAPH = re.compile(r"(?<!:)\bp[0-9]{4,}\b")
PARAGRAPH_RANGE = re.compile(r"\bp([0-9]{4,})\s*[-–—]\s*p([0-9]{4,})\b")
EXPERIMENT_LABEL = re.compile(r"(?<![A-Za-z0-9])(?:A[01]V[012]D[012]|[EV][012])(?![A-Za-z0-9])")
CLASSIFICATION_GUIDANCE = {
    "version": "cuad-definitions-study-boundary-1",
    "canonical_source": {
        "path": "data/raw/cuad/extracted/CUADv1.json",
        "sha256": "ed0b77d85bdf4014d7495800e8e4a70565b48ee6f8a2e5dca9cf8655dbf10eae",
        "locator": "Unique Details text of each named category's canonical question; extracted without answers or case identifiers.",
    },
    "verbatim_canonical_details": {
        "Termination For Convenience": "Can a party terminate this\u00a0 contract without cause (solely by giving a notice and allowing a waiting\u00a0 period to expire)?",
        "Change Of Control": "Does one party have the right to terminate or is consent or notice required of the counterparty if such party undergoes a change of control, such as a merger, stock sale, transfer of all or substantially all of its assets or business, or assignment by operation of law?",
    },
    "study_boundary_source": {
        "path": "outputs/recovery_v4/preflight/medium_replacement_review.md",
        "sha256": "4bf729c7b8a6e403a300db551534f415032aea03e5f0205203603d909cfeb057",
        "line": 17,
        "verbatim_excerpts_case_parentheticals_omitted": [
            "A generic anti-assignment provision plus an ordinary successors clause is retained with a boundary flag where no provision expressly links the corporate event to consent, notice or termination",
            "An explicit operation-of-law prohibition/consent creates enough direct canonical uncertainty to exclude pending adjudication",
            "This distinction is an AI operational interpretation, not a legal conclusion.",
        ],
    },
    "review_application": [
        "Use the supplied canonical questions as category definitions. The policy action mapping and the frozen-label score remain separate from this supplementary semantic review.",
        "Under this study's operational interpretation, generic assignment or successor wording alone does not automatically establish Change Of Control. Examine whether the text actually links a counterparty control-related event to termination, consent or notice.",
        "Do not automatically accept or reject assignment-by-operation-of-law, merger/successor, or similar boundary wording. If the trigger or exception is unresolved, record ambiguous and quote the competing provisions; do not alter the original label.",
        "The source-only review's exclusion wording records a pre-execution eligibility decision, not authority for excluding an observed result or changing its score. Neither the source review nor this guide is a legal rule.",
    ],
}
SELECTION_RULES = {
    "version": "semantic-review-selection-1",
    "e1_recovery": "Select every E1 run with a present target in either accepted Manager or accepted Compliance output, including recovery later lost or not delivered.",
    "v2_v1_disagreement": "Select both members of every same-case, same-error-condition, same-repeat A1V2D0/A1V1D0 pair if (terminal execution status, delivered Compliance target status map, delivered decision) differs. Missing accepted final output is null; different reasoning prose or tool counts alone is not a disagreement.",
    "negative_false_positive": "Select every negative-sentinel run where either accepted role output marks any policy category present although its frozen gold label is absent.",
    "delivery_failure": "Select every terminal failed run, and every completed run lacking an accepted Compliance output.",
    "deduplication": "Export each selected run once; retain all applicable reasons privately.",
    "completion_gate": "All jobs in the supplied frozen plan must have an identity-matching completed/failed run.json before any output is written. Unplanned artifacts are refused.",
    "primary_endpoint": "This semantic-review sampling rule does not alter the planned-denominator primary endpoint or its scoring labels.",
}
LIMITATIONS = [
    "Experimental condition labels, gold labels, selection reasons, run/case/contract identifiers and pair membership are withheld. Identical original contract content remains recognizable.",
    "Model reason summaries, uncertainties and behavior may allow reviewers to infer a treatment. This is metadata blinding, not guaranteed concealment of treatment from prose.",
    "Only referenced original paragraphs are supplied, not the complete contract. Do not infer contract-wide absence from this subset; use insufficient_evidence when needed.",
    "Full source paragraphs are supplied to the reviewer even when the model only saw a snippet or an identifier. Check the separate actual-visibility fields before attributing evidence access.",
    "An identifier absent from a structured observation ledger may still have appeared in narrative text. Exact text matching is an observation about saved inputs, not a claim about model comprehension.",
    "No hidden reasoning trace is exported. Accepted final summaries and uncertainties are model assertions, not proof of their factual or procedural claims.",
]
GUIDANCE = """# Blinded semantic review

Use only your assigned reviewer JSON and this guide. Do not inspect private
mapping files, raw runs, manifests, prior analyses or other reviewers' findings.
This is an AI review, not a human adjudication or a replacement for frozen gold.

Review each role's accepted final judgment against the verbatim source paragraphs
and policy. Keep clause existence, policy action, actual source visibility,
provenance claims and final delivery separate. Last parsed candidates, when
present, are explicitly unaccepted and must not be called delivered answers.
An alternative paragraph may support a judgment even when it differs from an
unknown reference annotation. An absent flag in the structured ledger does not
establish that the corresponding text was never visible.

Keep all records for one contract together. For each review_id and role, return:
target category; assessment (supported / contradicted / ambiguous /
insufficient_evidence); policy-action consistency; source paragraph indices and
exact character offsets; short supporting quotation; source access actually
visible to that role; any overstated coverage or unsupported provenance claim;
delivery status; and unresolved ambiguity. Do not guess experimental conditions
or revise labels. Absence needs contract-wide evidence that this cited subset may
not contain. Distinguish model claim from your source-based assessment.
"""


def reviewer_guide():
    lines = [GUIDANCE, "\n## Classification definitions and study boundary\n",
             "These are the supplied CUAD classification definitions and this study's operational review convention, not legal advice or legal rules. Preserve original-label scoring.\n"]
    for category, definition in CLASSIFICATION_GUIDANCE["verbatim_canonical_details"].items():
        lines.extend([f"### {category}\n", f"> {definition}\n"])
    canonical = CLASSIFICATION_GUIDANCE["canonical_source"]
    lines.append(f"Definition evidence: `{canonical['path']}`, SHA256 `{canonical['sha256']}`; category question Details text copied verbatim, without answers.\n")
    lines.append("Source-only pre-execution review excerpts below omit case-identifying parentheticals; quoted text is otherwise verbatim.\n")
    for excerpt in CLASSIFICATION_GUIDANCE["study_boundary_source"]["verbatim_excerpts_case_parentheticals_omitted"]:
        lines.append(f"> {excerpt}\n")
    boundary = CLASSIFICATION_GUIDANCE["study_boundary_source"]
    lines.append(f"Boundary evidence: `{boundary['path']}`, line {boundary['line']}, SHA256 `{boundary['sha256']}`. These citations are provenance only: do not open the unblinded source review or canonical answer labels.\n")
    lines.extend("- " + rule + "\n" for rule in CLASSIFICATION_GUIDANCE["review_application"])
    return "\n".join(lines)


def terminal_artifacts(plan, registry, raw):
    """Validate all planned envelopes before reading traces or creating output."""
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(registry.to_payload(), sort_keys=True).encode()).hexdigest()
    if not plan.registry_fingerprint or fingerprint != plan.registry_fingerprint:
        raise ValueError("review requires the registry pinned by the frozen plan")
    extra = sorted(p.parent.name for p in raw.glob("*/run.json") if p.parent.name not in plan.run_ids)
    if extra:
        raise ValueError("raw tree contains runs outside the frozen plan")
    artifacts, unfinished = {}, []
    for job in plan.jobs:
        path = raw / job.run_id / "run.json"
        if not path.is_file():
            unfinished.append(job.run_id)
            continue
        artifact = RunArtifact.from_payload(json.loads(path.read_text(encoding="utf-8")))
        if artifact.execution.status.value not in ("completed", "failed"):
            unfinished.append(job.run_id)
            continue
        for field in ("run_id", "experiment_id", "case_id", "contract_id", "condition_id",
                      "error_condition", "repeat_index", "source_access", "verification_required",
                      "recovery_required", "models_version", "model_fingerprint"):
            if getattr(artifact, field) != getattr(job, field):
                raise ValueError(f"artifact {field} differs from frozen plan: {job.run_id}")
        expected_runtime = plan.runtime_config.model_dump(mode="json") if plan.runtime_config else None
        if (artifact.plan_fingerprint != plan.fingerprint()
                or artifact.profile_fingerprint != plan.profile_fingerprint
                or artifact.runtime_config != expected_runtime
                or artifact.disclosure_level != (plan.disclosure_levels or {}).get(job.condition_id)):
            raise ValueError(f"artifact configuration differs from frozen plan: {job.run_id}")
        artifacts[job.run_id] = artifact
    if unfinished:
        raise ValueError(f"formal review export refused: {len(unfinished)} planned runs are not terminal; first: {unfinished[0]}")
    return artifacts


def outcome_signature(artifact):
    execution = artifact.execution
    final = execution.compliance_output if execution.status.value == "completed" else None
    return (execution.status.value,
            tuple(sorted((key, value.value) for key, value in final.target_clause_status.items())) if final else None,
            final.decision.value if final else None)


def select_runs(plan, registry, artifacts):
    selected = defaultdict(set)
    pairs = defaultdict(dict)
    for job in plan.jobs:
        artifact, case = artifacts[job.run_id], registry.get(job.case_id)
        outputs = [getattr(artifact.execution, role + "_output") for role in ROLES]
        if job.error_condition.value == "E1" and any(
            out and out.target_clause_status.get(case.target_category)
            and out.target_clause_status[case.target_category].value == "present" for out in outputs
        ):
            selected[job.run_id].add("e1_recovery_any_role")
        if case.is_negative_sentinel and any(
            out and any(status.value == "present" and case.gold_status_for(category).value == "absent"
                        for category, status in out.target_clause_status.items()
                        if category in case.policy.target_clause_categories) for out in outputs
        ):
            selected[job.run_id].add("negative_false_positive_any_role")
        if artifact.execution.status.value == "failed" or artifact.execution.compliance_output is None:
            selected[job.run_id].add("delivery_failure")
        if job.condition_id in ("A1V1D0", "A1V2D0"):
            key = (job.case_id, job.error_condition.value, job.repeat_index)
            if job.condition_id in pairs[key]:
                raise ValueError("duplicate V2/V1 pairing cell")
            pairs[key][job.condition_id] = job.run_id
    for pair in pairs.values():
        if set(pair) != {"A1V1D0", "A1V2D0"}:
            raise ValueError("V2/V1 review pair is missing its planned counterpart")
        first, second = pair["A1V1D0"], pair["A1V2D0"]
        if outcome_signature(artifacts[first]) != outcome_signature(artifacts[second]):
            selected[first].add("v2_v1_final_disagreement")
            selected[second].add("v2_v1_final_disagreement")
    return {key: sorted(value) for key, value in selected.items()}


def opaque_id(secret, namespace, value):
    return hmac.new(secret, (namespace + ":" + value).encode(), hashlib.sha256).hexdigest()[:20]


def anonymize(value, *, job, contract_key, document_prefix, secret):
    if isinstance(value, dict):
        return {key: anonymize(item, job=job, contract_key=contract_key,
                              document_prefix=document_prefix, secret=secret)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [anonymize(item, job=job, contract_key=contract_key,
                         document_prefix=document_prefix, secret=secret) for item in value]
    if not isinstance(value, str):
        return value
    value = FULL_PARAGRAPH.sub(lambda match: (
        contract_key if match[1] == document_prefix else "foreign-" + opaque_id(secret, "source", match[1]))
        + ":" + match[2], value)
    for original, replacement in ((job.run_id, "[review record]"), (job.contract_id, contract_key),
                                  (job.case_id, "[case]"), (job.experiment_id, "[study]")):
        value = value.replace(original, replacement)
    value = re.sub(r"\b(?:CASE|CUAD)-\d+\b", "[source identity]", value)
    return EXPERIMENT_LABEL.sub("[condition withheld]", value)


def reference_ids(outputs, document):
    serialized = json.dumps(outputs, ensure_ascii=False)
    prefix = document.paragraphs[0].paragraph_id.split(":")[0]
    ids = {match.group() for match in FULL_PARAGRAPH.finditer(serialized)}
    ids.update(prefix + ":" + match.group() for match in SHORT_PARAGRAPH.finditer(serialized))
    for match in PARAGRAPH_RANGE.finditer(serialized):
        start, end = int(match[1]), int(match[2])
        if 0 <= end - start <= 10000:
            ids.update(f"{prefix}:p{index:04d}" for index in range(start, end + 1))
    return sorted(ids)


def text_seen(text, calls):
    if not text:
        return []
    needles = {text, json.dumps(text, ensure_ascii=False)[1:-1],
               json.dumps(text, ensure_ascii=True)[1:-1]}
    return [{"invocation": call.get("invocation"), "call_index": call.get("call_index")}
            for call in calls if any(needle in rendered_input(call) for needle in needles)]


def source_visibility(pid, paragraph, case, role, models, tools, verifications):
    calls = [call for call in models if call.get("role") == role]
    own_tools = [tool for tool in tools if tool.get("node") == role and tool.get("ok") is True]
    final_input = calls[-1:]  # Last sent input; not a claim that a final answer succeeded.
    last_input_available = bool(final_input) and isinstance(final_input[0].get("rendered_input"), str)
    successful = [tool for tool in own_tools if pid in (tool.get("result_ids") or [])]
    fragments = []
    for tool in successful:
        for locator in tool.get("result_locators") or []:
            if locator.get("paragraph_id") != pid:
                continue
            if tool.get("tool") == "open_source_span":
                # Real open records carry excerpt_* = null explicitly.
                start, end = locator.get("start_char"), locator.get("end_char")
            else:
                # A search hit is only the logged excerpt, never an assumed
                # full paragraph if its excerpt offsets are unavailable.
                start, end = locator.get("excerpt_start_char"), locator.get("excerpt_end_char")
            valid = type(start) is int and type(end) is int and 0 <= start < end <= len(case.contract_text)
            text = case.contract_text[start:end] if valid else None
            fragments.append({"tool_sequence": tool.get("sequence"), "request_call_index": tool.get("call_index"),
                              "invocation": tool.get("invocation"), "tool": tool.get("tool"),
                              "start_char": start, "end_char": end, "locator_valid": valid,
                              "text": text, "visible_input_calls": text_seen(text, calls),
                              "visible_in_last_sent_input": bool(text_seen(text, final_input)) if text and last_input_available else None})
    full_text = paragraph.text if paragraph else None
    verdict = next((v for v in reversed(verifications) if v.get("node") == role), {})
    return {"input_visibility_scope": {"logged_model_calls": len(calls),
                                      "inspectable_rendered_inputs": sum(isinstance(call.get("rendered_input"), str) for call in calls),
                                      "last_sent_input_available": last_input_available},
            "own_search_or_page_returned_logged": any(t.get("tool") in ("search_contract", "next_search_page") for t in successful),
            "own_successful_open_logged": any(t.get("tool") == "open_source_span" for t in successful),
            "full_id_visible_input_calls": text_seen(pid, calls),
            "shorthand_visible_input_calls": text_seen(pid.split(":")[-1], calls),
            "full_paragraph_visible_input_calls": text_seen(full_text, calls),
            "full_paragraph_visible_in_last_sent_input": bool(text_seen(full_text, final_input)) if full_text and last_input_available else None,
            "logged_tool_fragments": fragments,
            "structured_citation_observations": [entry for entry in verdict.get("citation_audit", [])
                                                 if entry.get("source_id") == pid]}


def review_item(job, artifact, registry, raw, secret):
    case, document = registry.get(job.case_id), registry.document(job.case_id)
    directory = raw / job.run_id
    models, tools, verifications = [read_jsonl(directory / name) for name in
                                   ("model_calls.jsonl", "tool_calls.jsonl", "verification.jsonl")]
    for record in models + tools + verifications:
        if record.get("run_id") not in (None, job.run_id):
            raise ValueError("cross-run record in review inputs")
    contract_key = "contract-" + opaque_id(secret, "contract", job.contract_id)
    review_id = "review-" + opaque_id(secret, "review", job.run_id)
    def mask(value):
        return anonymize(value, job=job, contract_key=contract_key, secret=secret,
                         document_prefix=document.paragraphs[0].paragraph_id.split(":")[0])
    outputs = {}
    for role in ROLES:
        out = getattr(artifact.execution, role + "_output")
        candidate = next((call.get("parsed_output") for call in reversed(models)
                          if call.get("role") == role and isinstance(call.get("parsed_output"), dict)), None)
        outputs[role] = {"accepted_output": out.model_dump(mode="json") if out else None,
                         "last_unaccepted_parsed_candidate": candidate if out is None else None}
    source_ids = reference_ids(outputs, document)
    sources = []
    for pid in source_ids:
        paragraph = document.paragraph(pid)
        visibility = {}
        for role in ROLES:
            original = source_visibility(pid, paragraph, case, role, models, tools, verifications)
            visibility[role] = mask(original)
            # Keep actual source snippets verbatim even if the source happens
            # to use a token that resembles an experiment label.
            for fragment, blinded in zip(original["logged_tool_fragments"], visibility[role]["logged_tool_fragments"]):
                blinded["text"] = fragment["text"]
        sources.append({"paragraph_id": mask(pid), "original_paragraph_index": paragraph.ordinal if paragraph else None,
                        "start_char": paragraph.start_char if paragraph else None,
                        "end_char": paragraph.end_char if paragraph else None,
                        "exists_in_this_contract": paragraph is not None,
                        "verbatim_text": paragraph.text if paragraph else None,
                        "actual_visibility": visibility})
    integrity = log_integrity(directory, artifact.model_dump(mode="json"), models, tools, verifications)
    # Correspondence diagnostics contain internal request/model IDs; the full
    # audit belongs in the separate offline report, not in the blinded pack.
    safe_integrity = {key: integrity[key] for key in (
        "log_files_present", "audit_field_schema_issues", "artifact_counts_match_logs",
        "observed_log_counts", "actual_requested_tool_calls", "request_execution_counts_equal")}
    item = {"review_id": review_id, "contract_key": contract_key,
            "policy": case.policy.model_dump(mode="json"),
            "delivery_status": artifact.execution.status.value,
            "final_delivered": artifact.execution.status.value == "completed" and artifact.execution.compliance_output is not None,
            "role_judgments": mask(outputs), "referenced_sources": sources,
            "log_integrity": safe_integrity,
            "failure": mask({"type": artifact.failure.failure_type, "stage": artifact.failure.failure_stage,
                             "message": artifact.failure.error}) if artifact.failure else None}
    return item


def write_pack(plan, registry, raw, output, *, reviewers=3):
    raw, output = Path(raw), Path(output)
    if reviewers < 1:
        raise ValueError("reviewers must be positive")
    if output.exists() and any(output.iterdir()):
        raise ValueError("review output must be new or empty; preserve prior review evidence")
    artifacts = terminal_artifacts(plan, registry, raw)
    selection = select_runs(plan, registry, artifacts)
    secret = secrets.token_bytes(32)
    items, mappings = [], []
    for job in plan.jobs:
        if job.run_id not in selection:
            continue
        item = review_item(job, artifacts[job.run_id], registry, raw, secret)
        items.append(item)
        mappings.append({"review_id": item["review_id"], "contract_key": item["contract_key"],
                         **job.model_dump(mode="json"), "selection_reasons": selection[job.run_id]})
    # Opaque order is unrelated to frozen execution order or condition identity.
    items.sort(key=lambda item: item["review_id"])
    contract_groups = defaultdict(list)
    for item in items:
        contract_groups[item["contract_key"]].append(item)
    assignments = [[] for _ in range(reviewers)]
    for group in sorted(contract_groups.values(), key=lambda group: (-len(group), group[0]["contract_key"])):
        min(assignments, key=len).extend(group)
    metadata = {"format_version": 1, "limitations": LIMITATIONS,
                "classification_guidance": CLASSIFICATION_GUIDANCE,
                "review_type": "AI semantic source review; metadata-blinded",
                "source_offsets": "Original contract character offsets, half-open [start_char, end_char). Original paragraph ordinal is preserved.",
                "assignment": "Every selected record appears in one shard; all records of one contract remain together. This is not three independent ratings per record."}
    private = {"selection_rules": SELECTION_RULES, "plan_fingerprint": plan.fingerprint(),
               "registry_fingerprint": plan.registry_fingerprint, "profile_fingerprint": plan.profile_fingerprint,
               "planned_terminal_runs": len(artifacts), "selected_runs": len(items),
               "selected_contracts": len(contract_groups),
               "selection_reason_counts": dict(Counter(reason for reasons in selection.values() for reason in reasons)),
               "mapping_secret_hex": secret.hex(), "mapping": mappings,
               "reviewer_assignments": {f"reviewer_{i}": [item["review_id"] for item in group]
                                        for i, group in enumerate(assignments, 1)}}
    # Every validation, selection and source extraction must finish before write.
    for folder in ("blinded", "private"):
        (output / folder).mkdir(parents=True, exist_ok=True)
    def write(path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write(output / "blinded" / "review_pack.json", {**metadata, "items": items})
    for i, group in enumerate(assignments, 1):
        write(output / "blinded" / f"reviewer_{i}.json", {**metadata, "items": group})
    (output / "blinded" / "REVIEW_GUIDE.md").write_text(reviewer_guide(), encoding="utf-8")
    write(output / "private" / "review_mapping.json", private)
    write(output / "private" / "selection_rules.json", SELECTION_RULES)
    return private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "registry", "profile", "raw", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--reviewers", type=int, default=3)
    args = parser.parse_args()
    plan, registry = RunPlan.load_json(args.plan), CaseRegistry.load_json(args.registry)
    profile = ExperimentProfile(args.profile)
    plan.check(profile.conditions, registry)
    if profile.fingerprint() != plan.profile_fingerprint:
        raise ValueError("profile differs from frozen plan")
    result = write_pack(plan, registry, args.raw, args.output, reviewers=args.reviewers)
    print(f"Exported {result['selected_runs']} selected records from {result['planned_terminal_runs']} terminal planned runs.")
    print("Give reviewers only blinded/ files. Keep private/ inaccessible to the reviewers.")


if __name__ == "__main__":
    main()

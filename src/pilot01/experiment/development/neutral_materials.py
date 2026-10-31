"""Research-side neutral material construction. Never imported by a model view."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...schemas import AnalystMemo, MemoClaim, ClauseStatus, ErrorCondition
from ..cases import CaseRegistry, CaseSpec


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def neutral_id_mapping(registry, seed=20261017):
    ids = sorted(registry.case_ids, key=lambda cid: hashlib.sha256(f"{seed}:{cid}".encode()).digest())
    return {cid: f"CASE-{i:04d}" for i, cid in enumerate(ids, 1)}


class NeutralCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_id: str
    case_id: str
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    quotes: dict[str, str]
    contract_text_hash: str
    model_id: str
    prompt_version: str
    generation_record_sha256: str
    generation_record_file: str = Field(default="", exclude_if=lambda value: not value)
    approved: bool = False
    reviewer: str = ""
    review_kind: Literal["human", "ai"] = "human"
    review_record_sha256: str = ""
    review_record_file: str = ""
    review_notes: str = ""
    reviewed_content_sha256: str = ""

    def content_fingerprint(self):
        return digest(self.model_dump(exclude={"approved", "reviewer", "review_kind", "review_record_sha256", "review_record_file", "review_notes", "reviewed_content_sha256"}))


def prohibited_paragraphs(case, document):
    return {p.paragraph_id for p in document.paragraphs
            if any(start < p.end_char and p.start_char < end for start, end in case.gold_spans())}


def check_candidate(candidate, *, case, document, neutral_case_id):
    errors = []
    if candidate.case_id != neutral_case_id or candidate.contract_text_hash != case.contract_text_hash:
        errors.append("case or contract hash mismatch")
    if candidate.category in case.policy.target_clause_categories:
        errors.append("candidate is a policy target")
    if not candidate.approved or not candidate.reviewer.strip() or not candidate.review_notes.strip():
        errors.append("neutrality/source review incomplete")
    if candidate.review_kind == "ai" and not re.fullmatch(r"sha256:[a-f0-9]{64}", candidate.review_record_sha256):
        errors.append("AI review record provenance missing")
    if candidate.reviewed_content_sha256 != candidate.content_fingerprint():
        errors.append("review is not bound to this exact candidate")
    if not candidate.model_id or not candidate.prompt_version or not candidate.generation_record_sha256.startswith("sha256:"):
        errors.append("generation provenance missing")
    if not candidate.quotes:
        errors.append("no source quotes")
    banned = prohibited_paragraphs(case, document)
    for pid, quote in candidate.quotes.items():
        p = document.paragraph(pid)
        if p is None or not quote.strip() or quote not in p.text:
            errors.append("quote is not an exact passage in this contract")
        if pid in banned:
            errors.append("full cited paragraph overlaps target evidence")
    # A warning that requires exclusion here; manual review also covers paraphrases/cross references.
    text = " ".join([candidate.summary, *(document.paragraph(pid).text for pid in candidate.quotes if document.paragraph(pid))])
    if re.search(r"change[ -]of[ -]control|termination for convenience|terminate.{0,30}(without cause|for any reason)", text, re.I):
        errors.append("explicit target wording in candidate or full source paragraph")
    return sorted(set(errors))


def export_candidate_tasks(registry, out: Path, seed=20261017):
    from ...prompts import load_prompt
    out.mkdir(parents=True, exist_ok=True)
    mapping = neutral_id_mapping(registry, seed)
    tasks = []
    prompt = load_prompt("neutral_claim_candidates", "v1")
    for case in registry.cases:
        doc = registry.document(case.case_id)
        tasks.append({"case_id": mapping[case.case_id], "contract_text_hash": case.contract_text_hash,
                      "candidate_limit": 8, "max_generation_attempts": 1,
                      "prompt_version": prompt.ref, "system": prompt.text,
                      "source": [{"paragraph_id": p.paragraph_id, "text": p.text} for p in doc.paragraphs]})
    (out / "candidate_tasks.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    (out / "researcher_id_mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    label_policy = {"basis": "original CUAD annotations via frozen source registry",
                    "action": "retain all source labels and cases; no additional sentinel adjudication"}
    (out / "label_policy.json").write_text(json.dumps(label_policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return tasks


def build_clean_registry(registry, candidates, *, seed=20261017):
    mapping = neutral_id_mapping(registry, seed)
    cases, qc, blocked = [], [], []
    for case in registry.cases:
        cid = mapping[case.case_id]
        doc = registry.document(case.case_id)
        accepted = []
        errors = []
        related = [c for c in candidates if c.case_id == cid]
        if len({c.candidate_id for c in related}) != len(related):
            errors.append("duplicate candidate identifiers")
        for c in related:
            if c.approved:
                candidate_errors = check_candidate(c, case=case, document=doc, neutral_case_id=cid)
                if candidate_errors:
                    errors.append(f"approved candidate {c.candidate_id} invalid: " + "; ".join(candidate_errors))
                else:
                    accepted.append(c)
        accepted.sort(key=lambda c: c.candidate_id)
        required = 3 if case.is_negative_sentinel else 4
        if len(accepted) < required:
            errors.append(f"needs {required} reviewed clean candidates; has {len(accepted)}")
        chosen = accepted[:required]
        if len({" ".join(c.summary.lower().split()) for c in chosen}) != len(chosen):
            errors.append("duplicate neutral assertions")
        if errors:
            blocked.append({"case_id": cid, "reasons": errors})
            continue
        targets = [c for c in case.memo.claims if c.category in case.policy.target_clause_categories]
        if len(targets) != 2:
            blocked.append({"case_id": cid, "reasons": ["exactly two target claims required"]})
            continue
        claims = [c.model_copy(update={"claim_id": f"CLAIM-{i:02d}"}) for i, c in enumerate(targets, 1)]
        for i, c in enumerate(chosen[:3], 3):
            claims.append(MemoClaim(claim_id=f"CLAIM-{i:02d}", category=c.category, status=ClauseStatus.PRESENT,
                                    summary=c.summary, source_ids=tuple(c.quotes)))
        target_id, replacement = None, None
        if not case.is_negative_sentinel:
            if sum(v is ClauseStatus.PRESENT for v in case.gold_target_clause_status.values()) != 1:
                blocked.append({"case_id": cid, "reasons": ["positive pool requires exactly one present target"]})
                continue
            target_id = next(c.claim_id for c in claims if c.category == case.target_category)
            c = chosen[3]
            replacement = MemoClaim(claim_id=target_id, category=c.category, status=ClauseStatus.PRESENT,
                                    summary=c.summary, source_ids=tuple(c.quotes))
        memo = AnalystMemo(memo_id=cid.replace("CASE-", "MEMO-"), case_id=cid, claims=tuple(claims))
        payload = case.model_dump()
        payload.update(case_id=cid, material_version="2", memo=memo, omission_target_claim_id=target_id,
                       omission_replacement_claim=replacement)
        cases.append(CaseSpec.model_validate(payload))
        word_delta = (len(replacement.summary.split()) - len(memo.claim(target_id).summary.split())) if replacement else 0
        qc.append({"case_id": cid, "candidate_ids": [c.candidate_id for c in chosen],
                   "prohibited_paragraphs": sorted(prohibited_paragraphs(case, doc)),
                   "summary_word_delta": word_delta, "length_review_warning": abs(word_delta) > 8})
    if blocked:
        return None, {"status": "blocked", "blocked": blocked, "qc": qc, "mapping": mapping}
    clean = CaseRegistry(cases, **registry.chunking)
    for case in clean.cases:
        if case.is_negative_sentinel:
            continue
        e0, e1 = clean.memo(case.case_id, ErrorCondition.E0), clean.memo(case.case_id, ErrorCondition.E1)
        if len(e0.claims) != 5 or len(e1.claims) != 5 or sum(a != b for a, b in zip(e0.claims, e1.claims)) != 1:
            raise ValueError("memo pair must be 5-to-5 with one changed slot")
    return clean, {"status": "ready", "blocked": [], "qc": qc, "mapping": mapping}


def verify_ready_registry(path: Path):
    manifest_path = path.parent / "material_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ready" or manifest.get("registry_sha256") != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError("clean material manifest is missing, blocked, or changed")
    for relative, sha in manifest.get("review_file_hashes", {}).items():
        file = (path.parent / relative).resolve()
        if not file.is_relative_to(path.parent.resolve()) or "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest() != sha:
            raise ValueError("material review input changed")
    registry = CaseRegistry.load_json(path)
    for case in registry.cases:
        if case.material_version != "2" or not re.fullmatch(r"CASE-[0-9]{4,}", case.case_id):
            raise ValueError("registry is not a clean v2 material set")
    return registry

"""Explicit one-call-per-case AI review; preserves raw evidence and never retries."""
import argparse
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool, Field

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.neutral_materials import NeutralCandidate, neutral_id_mapping


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    supported: StrictBool
    neutral: StrictBool
    distinct: StrictBool
    reason: str = Field(min_length=1)


def validate_assessments(payload, candidates):
    if set(payload) != {"assessments"} or not isinstance(payload["assessments"], list):
        raise ValueError("expected an assessments array")
    rows = [Assessment.model_validate(row) for row in payload["assessments"]]
    if len(rows) != len(candidates) or {r.candidate_id for r in rows} != {c.candidate_id for c in candidates}:
        raise ValueError("AI reviewer omitted, repeated or invented candidate IDs")
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--seed", type=int, default=20261017)
    p.add_argument("--live", action="store_true")
    p.add_argument("--preparation-config", type=Path, help="independent material preparation model settings")
    args = p.parse_args()
    if not args.live:
        p.error("AI review spends API tokens; explicitly pass --live")
    out = Path(args.out)
    path = out / f"review_{args.case}.jsonl"
    if path.exists():
        p.error("one AI review attempt already recorded; no automatic retries")
    candidates = [NeutralCandidate.model_validate_json(line) for line in
        (out / f"candidates_{args.case}.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()]
    registry = CaseRegistry.load_json(args.registry)
    mapping = neutral_id_mapping(registry, args.seed)
    case = next(c for c in registry.cases if mapping[c.case_id] == args.case)
    document = registry.document(case.case_id)
    paragraphs = {}
    for c in candidates:
        for pid in c.quotes:
            paragraph = document.paragraph(pid)
            paragraphs[pid] = paragraph.text if paragraph else None
    from pilot01.experiment.development.preparation_config import load_preparation_config
    from pilot01.prompts import load_prompt
    from pilot01.model import ModelMessage, ModelRequest, ModelCallLog, record_call, extract_json_object
    from pilot01.model.openai_compat import OpenAICompatibleClient
    prompt = load_prompt("neutral_claim_review", "v1")
    preparation = load_preparation_config(args.preparation_config)
    request = ModelRequest(messages=(ModelMessage(role="system", content=prompt.text),
        ModelMessage(role="user", content=json.dumps({"case_id": args.case,
            "candidates": [{"candidate_id": c.candidate_id, "category": c.category, "summary": c.summary,
                            "quotes": c.quotes} for c in candidates], "full_paragraphs": paragraphs}, ensure_ascii=False))),
        params=preparation.model, role="material_reviewer", prompt_version=prompt.ref, invocation=0)
    client = OpenAICompatibleClient()
    log = ModelCallLog(path=path)
    try:
        response = client.generate(request)
    except Exception as exc:
        log.append(record_call(index=0, request=request, prompt_version=prompt.ref, attempt=0,
                               is_repair=False, error=str(exc)))
        raise
    log.append(record_call(index=0, request=request, prompt_version=prompt.ref, attempt=0,
                           is_repair=False, response=response))
    if response.finish_reason == "length":
        raise ValueError("AI review truncated; no retry or acceptance")
    rows = validate_assessments(json.loads(extract_json_object(response.text)), candidates)
    record_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    reviewed = []
    for c, row in zip(candidates, (next(r for r in rows if r.candidate_id == c.candidate_id) for c in candidates)):
        c = c.model_copy(update={"approved": row.supported and row.neutral and row.distinct,
            "review_kind": "ai", "reviewer": f"AI:{response.model_id}:{prompt.ref}",
            "review_record_sha256": record_hash, "review_notes": row.reason})
        c = c.model_copy(update={"reviewed_content_sha256": c.content_fingerprint()})
        reviewed.append(c)
    (out / f"reviewed_{args.case}.jsonl").write_text("".join(c.model_dump_json() + "\n" for c in reviewed), encoding="utf-8")
    print(json.dumps({"case_id": args.case, "reviewed": len(reviewed), "ai_approved": sum(c.approved for c in reviewed)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

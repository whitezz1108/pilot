"""Bounded, resumable material preparation. --live is required for any API call."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import time

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.neutral_materials import NeutralCandidate, check_candidate, neutral_id_mapping
from pilot01.experiment.development.preparation_config import load_preparation_config
from pilot01.model import ModelCallLog, ModelMessage, ModelRequest, record_call, extract_json_object
from pilot01.model.log import ModelCallRecord
from pilot01.prompts import load_prompt
from review_neutral_claims import validate_assessments


def hash_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_candidate(c, document):
    """Only relocate verbatim quotes when they have a unique exact paragraph match."""
    quotes, changes = {}, []
    for pid, quote in c.quotes.items():
        paragraph = document.paragraph(pid)
        if paragraph is not None and quote.strip() and quote in paragraph.text:
            actual = pid
        else:
            matches = [p for p in document.paragraphs if quote.strip() and quote in p.text]
            if len(matches) != 1:
                return c, [{"old_id": pid, "error": "quote has no unique exact source paragraph"}]
            actual = matches[0].paragraph_id
            changes.append({"old_id": pid, "new_id": actual, "quote": quote})
        if actual in quotes and quotes[actual] != quote:
            quotes[actual] = document.paragraph(actual).text
        else:
            quotes[actual] = quote
    c = c.model_copy(update={"quotes": quotes})
    return c.model_copy(update={"reviewed_content_sha256": c.content_fingerprint()}), changes


def source_errors(c, case, document, cid):
    # Pre-review source checks must not manufacture any semantic or human approval.
    return [e for e in check_candidate(c, case=case, document=document, neutral_case_id=cid)
            if e not in {"neutrality/source review incomplete", "AI review record provenance missing"}]


class Pipeline:
    def __init__(self, registry, out, preparation, seed):
        self.registry, self.out, self.preparation, self.seed = registry, out, preparation, seed
        mapping = neutral_id_mapping(registry, seed)
        self.cases = {mapping[c.case_id]: c for c in registry.cases}
        self.tasks = {t["case_id"]: t for t in (json.loads(line) for line in
            (out / "candidate_tasks.jsonl").read_text(encoding="utf-8").split("\n") if line.strip())}

    def call(self, path, *, role, prompt, payload):
        request = ModelRequest(messages=(ModelMessage(role="system", content=prompt.text),
            ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False))),
            params=self.preparation.model, role=role, prompt_version=prompt.ref, invocation=0)
        if path.exists():
            lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
            if len(lines) != 1:
                raise ValueError(f"incomplete or ambiguous existing call: {path.name}")
            record = ModelCallRecord.model_validate_json(lines[0])
            if (record.rendered_input != request.rendered_input() or record.params.model_dump() != request.params.model_dump()
                    or record.role != role or record.prompt_version != prompt.ref):
                raise ValueError("existing preparation call differs from frozen request/configuration")
        else:
            from pilot01.model.openai_compat import OpenAICompatibleClient
            log = ModelCallLog(path=path)
            try:
                response = OpenAICompatibleClient().generate(request)
            except Exception as exc:
                record = record_call(index=0, request=request, prompt_version=prompt.ref, attempt=0,
                                     is_repair=False, error=str(exc))
                log.append(record)
                raise
            record = record_call(index=0, request=request, prompt_version=prompt.ref, attempt=0,
                                 is_repair=False, response=response)
            log.append(record)
        if record.error or record.finish_reason == "length" or not record.raw_response:
            raise ValueError(f"unusable call {path.name}: {record.error or record.finish_reason}")
        return json.loads(extract_json_object(record.raw_response)), record

    def run_case(self, cid):
        start = time.monotonic()
        case, task = self.cases[cid], self.tasks[cid]
        document = self.registry.document(case.case_id)
        required = 3 if case.is_negative_sentinel else 4
        all_candidates, outcomes, normalization = [], [], []
        for attempt in (1, 2):
            suffix = "" if attempt == 1 else "_attempt2"
            gen_path = self.out / f"generation_{cid}{suffix}.jsonl"
            source = task["source"]
            prompt = load_prompt("neutral_claim_candidates", "v1")
            if attempt == 2:
                prompt = load_prompt("neutral_claim_candidates_recovery", "v1")
                risky = re.compile(r"change[ -]of[ -]control|terminat|assign|merger|acqui[rs]|ownership|shareholder|renew|expir|cancell|successor|transfer", re.I)
                eligible = [p for p in source if len(p["text"]) >= 200 and not risky.search(p["text"])]
                ids = {p["paragraph_id"] for p in sorted(eligible, key=lambda p: (len(p["text"]), p["paragraph_id"]))[:32]}
                source = [p for p in source if p["paragraph_id"] in ids]
            payload = {"case_id":cid,"contract_text_hash":case.contract_text_hash,"source":source}
            if attempt == 2:
                payload["already_proposed_summaries_to_avoid_repeating"] = [c.summary for c in all_candidates]
            try:
                data, record = self.call(gen_path, role="material_generator", prompt=prompt, payload=payload)
                if set(data) != {"candidates"} or not isinstance(data["candidates"],list) or len(data["candidates"])>8:
                    raise ValueError("expected at most eight candidate objects")
                raw = []
                for i, item in enumerate(data["candidates"], 1):
                    if set(item) != {"category","summary","quotes"}:
                        raise ValueError("unexpected candidate fields")
                    c = NeutralCandidate(candidate_id=f"{cid}-N{8*(attempt-1)+i:02d}",case_id=cid,
                        contract_text_hash=case.contract_text_hash,model_id=record.response_model_id or record.params.model_id,
                        prompt_version=prompt.ref,generation_record_sha256=hash_file(gen_path),
                        generation_record_file=gen_path.name if attempt>1 else "", **item)
                    raw.append(c.model_copy(update={"reviewed_content_sha256":c.content_fingerprint()}))
                (self.out/f"proposals_{cid}{suffix}.jsonl").write_text("".join(c.model_dump_json()+"\n" for c in raw),encoding="utf-8")
            except Exception as exc:
                outcomes.append({"attempt":attempt,"stage":"generation","error":str(exc)})
                continue
            eligible, rejected = [], []
            for original in raw:
                c, edits = normalize_candidate(original, document)
                errors = source_errors(c,case,document,cid)
                normalization.append({"candidate_id":c.candidate_id,"original":original.model_dump(mode="json"),
                                      "normalized":c.model_dump(mode="json"),"exact_quote_id_corrections":edits,"source_errors":errors})
                if errors:
                    rejected.append(c.model_copy(update={"review_kind":"ai","review_notes":"PROGRAM EXCLUSION; semantic review not performed: "+"; ".join(errors)}))
                else:
                    eligible.append(c)
            all_candidates.extend(rejected)
            if eligible:
                review_prompt = load_prompt("neutral_claim_review", "v1")
                review_payload = {"case_id":cid,"candidates":[{"candidate_id":c.candidate_id,"category":c.category,
                    "summary":c.summary,"quotes":c.quotes} for c in eligible],
                    "full_paragraphs":{pid:document.paragraph(pid).text for c in eligible for pid in c.quotes},
                    "previously_accepted_assertions":[c.summary for c in all_candidates if c.approved],
                    "additional_review_rule":"Reject candidates that restate any previously accepted assertion."}
                assessments, review_path, review_record = None, None, None
                for review_attempt in (1,2):
                    review_path = self.out/f"review_{cid}{suffix}{'' if review_attempt==1 else '_retry1'}.jsonl"
                    try:
                        data, review_record = self.call(review_path, role="material_reviewer",prompt=review_prompt,payload=review_payload)
                        assessments = validate_assessments(data,eligible)
                        break
                    except Exception as exc:
                        outcomes.append({"attempt":attempt,"stage":"review","review_attempt":review_attempt,"error":str(exc)})
                for c in eligible:
                    if assessments is None:
                        c = c.model_copy(update={"review_kind":"ai","review_notes":"AI review unavailable; not approved."})
                    else:
                        row = next(r for r in assessments if r.candidate_id==c.candidate_id)
                        c = c.model_copy(update={"approved":row.supported and row.neutral and row.distinct,
                            "review_kind":"ai","reviewer":f"AI:{review_record.response_model_id or review_record.params.model_id}:{review_prompt.ref}",
                            "review_record_sha256":hash_file(review_path),"review_record_file":review_path.name,"review_notes":row.reason})
                    c = c.model_copy(update={"reviewed_content_sha256":c.content_fingerprint()})
                    all_candidates.append(c)
            approved = [c for c in all_candidates if c.approved]
            if len(approved)>=required:
                break
        approved = [c for c in all_candidates if c.approved]
        for c in approved:
            errors = check_candidate(c,case=case,document=document,neutral_case_id=cid)
            if errors:
                raise ValueError(f"approved candidate invalid: {c.candidate_id}: {errors}")
        (self.out/f"normalization_{cid}.json").write_text(json.dumps(normalization,ensure_ascii=False,indent=2),encoding="utf-8")
        (self.out/f"reviewed_{cid}.jsonl").write_text("".join(c.model_dump_json()+"\n" for c in all_candidates),encoding="utf-8")
        result = {"case_id":cid,"status":"sufficient" if len(approved)>=required else "insufficient",
                  "required":required,"approved":len(approved),"candidates":len(all_candidates),
                  "outcomes":outcomes,"elapsed_seconds":round(time.monotonic()-start,2)}
        (self.out/f"status_{cid}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    p.add_argument("--preparation-config",type=Path)
    p.add_argument("--workers",type=int,default=6,choices=range(1,7))
    p.add_argument("--seed",type=int,default=20261017)
    p.add_argument("--case")
    p.add_argument("--live",action="store_true")
    args=p.parse_args()
    if not args.live:
        p.error("explicit --live required; only material preparation, no formal experiment")
    registry=CaseRegistry.load_json(args.source)
    pipeline=Pipeline(registry,args.out,load_preparation_config(args.preparation_config),args.seed)
    ids=[args.case] if args.case else sorted(pipeline.cases)
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(pipeline.run_case,cid):cid for cid in ids}
        for future in concurrent.futures.as_completed(futures):
            cid=futures[future]
            try:
                result=future.result()
            except Exception as exc:
                result={"case_id":cid,"status":"pipeline_error","error":str(exc)}
                (args.out/f"status_{cid}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            (args.out/"batch_status.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    return int(any(r["status"]!="sufficient" for r in results))


if __name__=="__main__":
    raise SystemExit(main())

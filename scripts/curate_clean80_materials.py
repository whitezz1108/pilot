"""Offline curation: source-bound explicit AI amendments, never a model call."""
import argparse
import hashlib
import json
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.neutral_materials import NeutralCandidate, neutral_id_mapping, check_candidate


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    p.add_argument("--seed",type=int,default=20261017)
    args=p.parse_args()
    registry=CaseRegistry.load_json(args.source)
    mapping=neutral_id_mapping(registry,args.seed)
    revision_path=args.out/"ai_review_revisions.json"
    revisions=json.loads(revision_path.read_text(encoding="utf-8")) if revision_path.exists() else {}
    all_rows,results,used=[],[],[]
    for case in registry.cases:
        cid=mapping[case.case_id]
        path=args.out/f"reviewed_{cid}.jsonl"
        required=3 if case.is_negative_sentinel else 4
        if not path.exists():
            results.append({"case_id":cid,"status":"awaiting_pipeline","required":required})
            continue
        document=registry.document(case.case_id)
        rows=[NeutralCandidate.model_validate_json(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
        for original in rows:
            c=original
            if c.candidate_id in revisions:
                revision=revisions[c.candidate_id]
                c=c.model_copy(update={k:revision[k] for k in ("summary","category","quotes") if k in revision})
                c=c.model_copy(update={"approved":True,"review_kind":"ai","reviewer":"AI:Codex","review_notes":revision["reason"]})
                c=c.model_copy(update={"reviewed_content_sha256":c.content_fingerprint()})
                content=c.model_dump(exclude={"approved","review_kind","reviewer","review_notes","reviewed_content_sha256","review_record_file","review_record_sha256"})
                record={"kind":"Explicit Codex AI source review; not human review","original_candidate":original.model_dump(mode="json"),
                        "reviewed_content":content,"reason":revision["reason"],"source_correction":revision.get("source_correction"),
                        "full_paragraphs":{pid:document.paragraph(pid).text for pid in c.quotes}}
                body=(json.dumps(record,ensure_ascii=False)+"\n").encode("utf-8")
                name=f"review_{c.candidate_id}_codex.jsonl"
                target=args.out/name
                if target.exists() and target.read_bytes()!=body:
                    raise ValueError("existing AI amendment changed; create a new version")
                target.write_bytes(body)
                c=c.model_copy(update={"review_record_file":name,"review_record_sha256":"sha256:"+hashlib.sha256(body).hexdigest()})
                used.append(c.candidate_id)
            if c.approved:
                errors=check_candidate(c,case=case,document=document,neutral_case_id=cid)
                if errors:
                    raise ValueError(f"approved candidate invalid: {c.candidate_id}: {errors}")
            all_rows.append(c)
        accepted=[c for c in all_rows if c.case_id==cid and c.approved]
        results.append({"case_id":cid,"required":required,"approved":len(accepted),
                        "status":"sufficient" if len(accepted)>=required else "insufficient"})
    (args.out/"reviewed_candidates.jsonl").write_text("".join(c.model_dump_json()+"\n" for c in all_rows),encoding="utf-8")
    result={"total_cases":len(registry.cases),"sufficient":sum(r["status"]=="sufficient" for r in results),
            "human_review_completed":False,"codex_review_amendments":used,"case_statuses":results}
    (args.out/"curation_status.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"total_cases":result["total_cases"],"sufficient":result["sufficient"],"insufficient":[r for r in results if r["status"]=="insufficient"]}))


if __name__=="__main__":
    main()

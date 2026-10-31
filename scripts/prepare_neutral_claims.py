"""Export neutral candidate tasks, or explicitly generate one case for review."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.development.neutral_materials import export_candidate_tasks, NeutralCandidate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=20261017)
    p.add_argument("--generate-case", help="one neutral case ID; only with --live")
    p.add_argument("--live", action="store_true")
    p.add_argument("--attempt", type=int, choices=(1, 2), default=1)
    p.add_argument("--preparation-config", type=Path, help="independent material preparation model settings")
    args = p.parse_args()
    out = Path(args.out)
    if not args.generate_case:
        if args.attempt != 1:
            p.error("--attempt 2 is only for a failed live generation")
        if args.live:
            p.error("--live requires --generate-case; task export is offline")
        if (out / "candidate_tasks.jsonl").exists():
            p.error("task package already exists; choose a new output directory")
        tasks = export_candidate_tasks(CaseRegistry.load_json(args.registry), out, args.seed)
        print(f"Exported {len(tasks)} offline tasks; no candidates generated or approved.")
        return 0
    if not args.live:
        p.error("generation spends API tokens; explicitly pass --live")
    tasks = [json.loads(line) for line in (out / "candidate_tasks.jsonl").read_text(encoding="utf-8").split("\n") if line.strip()]
    matches = [t for t in tasks if t["case_id"] == args.generate_case]
    if len(matches) != 1:
        p.error("unknown neutral case ID")
    task = matches[0]
    target = out / f"candidates_{task['case_id']}.jsonl"
    path = out / (f"generation_{task['case_id']}.jsonl" if args.attempt == 1 else f"generation_{task['case_id']}_attempt2.jsonl")
    if target.exists():
        p.error("candidates already exist; cannot regenerate successful material")
    if args.attempt == 2:
        first = out / f"generation_{task['case_id']}.jsonl"
        if not first.exists() or not first.stat().st_size:
            p.error("supplementary generation requires a completed first attempt")
        from pilot01.prompts import load_prompt
        prompt = load_prompt("neutral_claim_candidates_recovery", "v1")
        risky = re.compile(r"change[ -]of[ -]control|terminat|assign|merger|acqui[rs]|ownership|shareholder|renew|expir|cancell|successor|transfer", re.I)
        eligible = [p for p in task["source"] if len(p["text"]) >= 200 and not risky.search(p["text"])]
        selected_ids = {p["paragraph_id"] for p in sorted(eligible, key=lambda p: (len(p["text"]), p["paragraph_id"]))[:32]}
        task = {**task, "system": prompt.text, "prompt_version": prompt.ref,
                "source": [p for p in task["source"] if p["paragraph_id"] in selected_ids]}
        if not task["source"]:
            raise ValueError("no eligible complete paragraphs for supplementary generation")
    if path.exists():
        p.error("one generation attempt already recorded; no automatic retries")
    from pilot01.experiment.development.preparation_config import load_preparation_config
    from pilot01.model import ModelMessage, ModelRequest, ModelCallLog, record_call, extract_json_object
    from pilot01.model.openai_compat import OpenAICompatibleClient
    preparation = load_preparation_config(args.preparation_config)
    request = ModelRequest(messages=(ModelMessage(role="system", content=task["system"]),
        ModelMessage(role="user", content=json.dumps({k: task[k] for k in ("case_id", "contract_text_hash", "source")}, ensure_ascii=False))),
        params=preparation.model, role="material_generator", prompt_version=task["prompt_version"], invocation=0)
    client = OpenAICompatibleClient()
    log = ModelCallLog(path=path)
    try:
        response = client.generate(request)
    except Exception as exc:
        log.append(record_call(index=0, request=request, prompt_version=request.prompt_version, attempt=0,
                               is_repair=False, error=str(exc)))
        raise
    log.append(record_call(index=0, request=request, prompt_version=request.prompt_version, attempt=0,
                           is_repair=False, response=response))
    if response.finish_reason == "length":
        raise ValueError("candidate generation truncated; no retry or acceptance")
    payload = json.loads(extract_json_object(response.text))
    if set(payload) != {"candidates"} or not isinstance(payload["candidates"], list) or len(payload["candidates"]) > task["candidate_limit"]:
        raise ValueError("expected at most eight candidate objects")
    record_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    candidates = []
    for i, item in enumerate(payload["candidates"], 1):
        if set(item) != {"category", "summary", "quotes"}:
            raise ValueError("candidate has unexpected fields")
        c = NeutralCandidate(candidate_id=f"{task['case_id']}-N{i:02d}", case_id=task["case_id"],
            contract_text_hash=task["contract_text_hash"], model_id=response.model_id,
            prompt_version=task["prompt_version"], generation_record_sha256=record_hash, **item)
        if args.attempt == 2:
            c = c.model_copy(update={"generation_record_file": path.name})
        c = c.model_copy(update={"reviewed_content_sha256": c.content_fingerprint()})
        candidates.append(c)
    target.write_text("".join(c.model_dump_json() + "\n" for c in candidates), encoding="utf-8")
    print(f"Wrote {len(candidates)} unapproved candidates to {target}; review required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

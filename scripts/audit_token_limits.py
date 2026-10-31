"""Read-only token and truncation audit; excludes transport secrets and response text."""
import argparse
import json
from collections import Counter
from pathlib import Path


def audit(root):
    records = []
    for path in sorted(Path(root).glob("*/model_calls.jsonl")):
        with path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    r = json.loads(line)
                    records.append((path, n, r))
    result = {"calls": len(records), "finish_reasons": dict(Counter(str(r.get("finish_reason")) for _, _, r in records)), "roles": {}, "truncations": []}
    for role in ("manager", "compliance"):
        rows = [r for _, _, r in records if r.get("role") == role]
        values = sorted(r["usage"]["completion_tokens"] for r in rows if r.get("usage") and r["usage"].get("completion_tokens") is not None)
        result["roles"][role] = {"calls": len(rows), "completion_p95": values[int((len(values)-1)*.95)] if values else None,
                                  "completion_max": max(values, default=None),
                                  "reasoning_detail_missing": sum((r.get("usage") or {}).get("reasoning_tokens") is None for r in rows)}
    for path, n, r in records:
        if r.get("finish_reason") == "length":
            result["truncations"].append({"path": path.as_posix(), "line": n, "role": r["role"], "usage": r.get("usage"),
                                          "visible_response_characters": len(r.get("raw_response") or ""), "params": r.get("params")})
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(audit(args.raw), ensure_ascii=False, indent=2), encoding="utf-8")

"""Freeze historical OpenAI-compatible wire requests into a replay corpus.

The builder never reconstructs a request from an aggregate. It requires the
actual payload for every request and fails closed on count, token totals,
required fields, and ambiguous metric alignment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_jsonl(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SystemExit(f"{path}:{line_no}: JSONL row is not an object")
            value["__source_file"] = str(path)
            value["__source_line"] = line_no
            rows.append(value)
    return rows


def load_metric_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix == ".jsonl":
            rows.extend(read_jsonl([path]))
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            selected = value
        elif isinstance(value, dict):
            selected = value.get("call_metrics") or value.get("calls")
            if not isinstance(selected, list):
                raise SystemExit(f"{path}: expected call_metrics/calls array")
        else:
            raise SystemExit(f"{path}: unsupported metrics JSON")
        if not all(isinstance(row, dict) for row in selected):
            raise SystemExit(f"{path}: metric rows must be objects")
        rows.extend(selected)
    return rows


def first_int(row: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        value: Any = row
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return int(value)
    return None


def extract_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if payload is None and isinstance(row.get("wire"), dict):
        payload = row["wire"].get("payload")
    if payload is None and isinstance(row.get("request"), dict):
        payload = row["request"].get("payload", row["request"])
    if not isinstance(payload, dict):
        raise SystemExit(f"missing actual request payload at {row.get('__source_file')}:{row.get('__source_line')}")
    return payload


def metric_identity(row: dict[str, Any]) -> tuple[Any, ...] | None:
    request_id = row.get("model_call_id") or row.get("request_id")
    if request_id:
        return ("id", request_id)
    if row.get("run_id") is not None and row.get("role") is not None and row.get("call_index") is not None:
        return ("position", row["run_id"], row["role"], int(row["call_index"]))
    return None


def wire_identity(row: dict[str, Any]) -> tuple[Any, ...] | None:
    request_id = row.get("model_call_id") or row.get("request_id")
    if request_id:
        return ("id", request_id)
    if row.get("run_id") is not None and row.get("role") is not None and row.get("call_index") is not None:
        return ("position", row["run_id"], row["role"], int(row["call_index"]))
    return None


def align_metrics(wire: list[dict[str, Any]], metrics: list[dict[str, Any]], align_by_order: bool = False) -> list[dict[str, Any]]:
    if len(wire) != len(metrics):
        raise SystemExit(f"wire/metric row count differs: {len(wire)} != {len(metrics)}")
    wire_ids = [wire_identity(row) for row in wire]
    metric_ids = [metric_identity(row) for row in metrics]
    if all(wire_ids) and all(metric_ids):
        by_id: dict[tuple[Any, ...], dict[str, Any]] = {}
        for key, row in zip(metric_ids, metrics):
            if key in by_id:
                raise SystemExit(f"duplicate metric identity: {key}")
            by_id[key] = row
        missing = [key for key in wire_ids if key not in by_id]
        if missing:
            raise SystemExit(f"metrics missing wire identities: {missing[:3]}")
        return [by_id[key] for key in wire_ids]
    if align_by_order:
        return metrics
    if any(wire_ids) or any(metric_ids):
        raise SystemExit("partial identities make positional metric alignment ambiguous")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire-jsonl", type=Path, action="append", required=True)
    parser.add_argument("--metrics", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmarks/native-sweep-v1"))
    parser.add_argument("--expected-requests", type=int, default=68)
    parser.add_argument("--expected-prompt-tokens", type=int, default=481507)
    parser.add_argument("--expected-completion-tokens", type=int, default=117593)
    parser.add_argument("--expected-reasoning-tokens", type=int, default=100285)
    parser.add_argument("--require-field", action="append", default=[])
    parser.add_argument("--align-by-order", action="store_true",
                        help="Explicitly attest that metrics have exactly the same order as wire rows")
    args = parser.parse_args()

    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty frozen workload directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    wire = read_jsonl(args.wire_jsonl)
    metrics = align_metrics(wire, load_metric_rows(args.metrics), args.align_by_order)
    if len(wire) != args.expected_requests:
        raise SystemExit(f"request count mismatch: expected {args.expected_requests}, got {len(wire)}")

    default_required = ["model", "messages", "temperature", "top_p", "top_k", "min_p", "max_tokens", "seed"]
    required = list(dict.fromkeys(default_required + args.require_field))
    records: list[dict[str, Any]] = []
    missing_fields: dict[str, int] = {}
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    for index, (wire_row, metric) in enumerate(zip(wire, metrics)):
        source_payload = extract_payload(wire_row)
        missing = [key for key in required if key not in source_payload]
        for key in missing:
            missing_fields[key] = missing_fields.get(key, 0) + 1
        if missing:
            raise SystemExit(f"request {index} missing required payload fields: {missing}")
        payload = json.loads(json.dumps(source_payload, ensure_ascii=False))
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        prompt = first_int(metric, ("prompt_tokens", "input_tokens", "native_input_ids", "usage.prompt_tokens"))
        completion = first_int(metric, ("completion_tokens", "output_tokens", "native_output_ids", "usage.completion_tokens"))
        reasoning = first_int(metric, ("reasoning_tokens", "usage.reasoning_tokens", "usage.completion_tokens_details.reasoning_tokens"))
        if None in (prompt, completion, reasoning):
            raise SystemExit(f"request {index} lacks complete prompt/completion/reasoning token metrics")
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["reasoning_tokens"] += reasoning
        identity = wire_identity(wire_row)
        records.append({
            "schema_version": "native-replay-request-v1",
            "sequence": index,
            "request_id": str((identity[-1] if identity and identity[0] == "id" else f"native-replay-{index:03d}")),
            "payload": payload,
            "source_payload_sha256": sha256_bytes(json.dumps(source_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
            "original_usage": {"prompt_tokens": prompt, "completion_tokens": completion, "reasoning_tokens": reasoning},
            "source": {"file": wire_row["__source_file"], "line": wire_row["__source_line"], "identity": identity},
        })

    expected_totals = {"prompt_tokens": args.expected_prompt_tokens,
                       "completion_tokens": args.expected_completion_tokens,
                       "reasoning_tokens": args.expected_reasoning_tokens}
    if totals != expected_totals:
        raise SystemExit(f"historical token totals mismatch: expected {expected_totals}, got {totals}")

    requests_path = args.output_dir / "requests.jsonl"
    requests_path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in records), encoding="utf-8")
    summary = {"schema_version": "native-replay-summary-v1", "request_count": len(records), **totals,
               "streaming_for_ttft": True, "missing_required_fields": missing_fields}
    summary_path = args.output_dir / "workload-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "native-replay-manifest-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "engineering replay; does not alter or add research runs",
        "request_order": "source order; identical for every cell",
        "sources": [{"path": str(path), "sha256": sha256_file(path)} for path in args.wire_jsonl + args.metrics],
        "files": {"requests.jsonl": sha256_file(requests_path), "workload-summary.json": sha256_file(summary_path)},
        "required_payload_fields": required,
        "expected_totals": expected_totals,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hashes = [f"{sha256_file(path)}  {path.name}" for path in (requests_path, summary_path, manifest_path)]
    (args.output_dir / "manifest.sha256").write_text("\n".join(hashes) + "\n", encoding="ascii")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()


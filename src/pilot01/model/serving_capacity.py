"""Validate actual SGLang capacity; no network access in this module."""

from .client import ModelClientError


def validate_sglang_capacity(info, endpoint):
    states = [info, *(info.get("internal_states") or [])]
    pools = [s["max_total_num_tokens"] for s in states
             if isinstance(s.get("max_total_num_tokens"), int)]
    if not pools or not isinstance(info.get("context_length"), int):
        raise ModelClientError("serving_capacity_failure: actual KV pool/context is missing; declared context is insufficient")
    actual, needed = min(pools), endpoint.context_window_tokens + endpoint.context_safety_tokens
    if actual < needed:
        raise ModelClientError(f"serving_capacity_failure: slot {endpoint.slot} actual KV tokens={actual}; "
                               f"context={endpoint.context_window_tokens} + safety={endpoint.context_safety_tokens} "
                               f"requires {needed}; provision actual KV capacity before launching")
    if info["context_length"] < endpoint.context_window_tokens:
        raise ModelClientError(f"serving_capacity_failure: slot {endpoint.slot} declares less context than the client")
    if (not isinstance(info.get("max_running_requests"), int)
            or info["max_running_requests"] < endpoint.max_in_flight):
        raise ModelClientError(f"serving_capacity_failure: slot {endpoint.slot} cannot serve its configured concurrency")
    input_limits = [s["max_req_input_len"] for s in states if isinstance(s.get("max_req_input_len"), int)]
    keys = ("context_length", "max_running_requests", "mem_fraction_static", "dtype", "quantization",
            "speculative_algorithm", "speculative_num_steps", "speculative_num_draft_tokens",
            "tool_call_parser", "reasoning_parser", "served_model_name")
    return {"capacity_version": "sglang-v1", **{k: info.get(k) for k in keys},
            "max_total_num_tokens": actual, "max_req_input_len": min(input_limits) if input_limits else None}

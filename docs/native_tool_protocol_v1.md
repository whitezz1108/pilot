# Native tool protocol v1

This protocol is an execution-layer change, not an AEVD treatment. All
treatments share the same provider-neutral decision types and source tools.

## Decision contract

Every model turn resolves to exactly one of:

- `ToolCallDecision(call_id, name, arguments)` from the provider's structured
  tool-call channel; or
- `FinalSubmissionDecision(text)` from normal assistant content.

Normal content and a tool call in the same turn, multiple calls in one turn, or
the retired `{"tool_request": ...}` text envelope in native mode are protocol
violations. They are recorded and terminate the run without format repair.
The same applies when the provider reports `finish_reason=tool_calls` without a
structured call, or reuses a call ID inside one node invocation.

## Tool results

Tool results return as `role=tool` messages linked by the provider `call_id`.
The model-call log and source-tool log both retain that id. Tool argument or
execution failures return an explicit error result; they are not silently
corrected.

## Pagination

`search_contract(query, target_category)` starts a search.
`next_search_page(cursor)` continues the exact server-bound search. A cursor is
opaque and cannot be paired with a new query or target category.

## Compatibility

Frozen experiments continue to use `tool_protocol: "2"` and
`tool_transport: legacy-text`. New runs must pair `tool_protocol: native-v1`
with an endpoint configured as `tool_transport: native`.

The native path is deliberately incompatible with the retired
`PILOT01_STRICT_TOOL_PROTOCOL` text grammar. If that environment variable is
still enabled, request construction fails before a model call instead of
silently combining two decoding mechanisms.

## SGLang endpoint requirement

The Qwen3.8 tokenizer template renders provider tools as
`<tool_call><function=...>` blocks. The isolated SGLang endpoint must therefore
set both parsers explicitly:

```text
--reasoning-parser qwen3
--tool-call-parser qwen3_coder
```

This pairing was checked offline against the installed SGLang 0.5.22 detector:
the model template exposes the tool schema, and `qwen3_coder` recovers the
function name and arguments from the generated tool block. Explicit launch
arguments are still required; auto-detection is not treated as a frozen
experiment setting.

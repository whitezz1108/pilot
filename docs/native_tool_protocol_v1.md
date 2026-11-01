# Native tool protocol v1

This protocol is an execution-layer change, not an AEVD treatment. All
treatments share the same provider-neutral decision types and source tools.

## Decision contract

Every model turn resolves to one of:

- `ToolCallDecision(calls)` containing one or more structured calls; or
- `FinalSubmissionDecision(text)` from normal assistant content.

Multiple calls in one response are supported and executed sequentially in provider
order. Each call consumes one unit of the node's tool budget. If the whole batch
would exceed the remaining budget, none of that batch is executed. At zero
remaining budget the next turn is final-only and carries no tool definitions.

Normal content mixed with calls, the retired `{"tool_request": ...}` text
envelope in native mode, `finish_reason=tool_calls` without a structured call,
and duplicate call IDs within a node are protocol violations. They terminate
the run without format repair. When a provider omits a call ID, the adapter
creates a deterministic correlation ID and records `adapter_generated` as its
origin; it does not repair tool names or arguments. Final answer schema repair
is a separate structured-output path.

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

The first isolated validation is summarized in
[`native_tool_protocol_validation_20261101.md`](native_tool_protocol_validation_20261101.md).

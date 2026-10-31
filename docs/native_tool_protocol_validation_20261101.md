# Native tool protocol validation — 2026-11-01

## Scope

This validates the new execution protocol only. It is not an AEVD treatment
run, does not use either frozen `tool_request: null` case, and cannot establish
that the historical null-generation tendency is fixed.

Code under live validation: `fe2fb91a975153f038c2798e36f4ce818d24b4b6` on
`codex/typed-tool-protocol`.

## Offline validation

- Windows isolated checkout: 367 protocol/model/source/node tests passed.
- Linux server isolated checkout: the same 367 tests passed.
- Three additional Linux checks passed for endpoint configuration, streamed
  tool-call reconstruction, and rejection of the retired grammar constraint.
- The repository-wide suite is not clean on the archived snapshot. Its
  remaining failures predate this branch and include excluded CUAD/generated
  data, stale prompt hashes and 16,384-token assertions, an existing transport
  error wrapping assertion, and an underspecified scripted-response fixture.

## Engine compatibility

- Engine image: SGLang 0.5.22, Transformers 5.19.0.
- Model: local BF16 Qwen3.8-27B with MTP/NEXTN.
- The tokenizer template contains native tools, reasoning effort, and
  `<tool_call><function=...>` instructions.
- The installed `qwen3_coder` detector recovered `search_contract` plus both
  typed arguments in an offline parser check.
- The isolated endpoint explicitly used `--reasoning-parser qwen3` and
  `--tool-call-parser qwen3_coder`, with radix cache disabled.

## Live synthetic smoke checks

The first request used an invented prompt and one invented tool schema. The
response had `finish_reason=tool_calls`, empty normal content, separately
captured reasoning, one provider call ID, and correctly parsed query and target
arguments. Streaming completed normally.

A second synthetic check exercised the complete loop against the test contract
library. A temporary four-tool smoke limit stopped the model at its fifth tool
request; the four dispatched calls had all succeeded. This was a test-fixture
mismatch because the real runtime limit is 16, not a parser or tool failure.

With the real 16-tool limit, the same synthetic task completed without format
repair:

- seven successful tool calls;
- an eighth model turn with `finish_reason=stop` and a valid final object;
- exact provider call-ID linkage between every assistant tool request and tool
  result;
- no null envelope, mixed content/tool turn, pagination error, or silent
  correction.

These were console-observed smoke results, not frozen provider-response
artifacts. They demonstrate end-to-end operability but are not a failure-rate
estimate or a root-cause experiment for the historical cases.

## Resource cleanup

The isolated GPU-2 container `pilot-native-tools-bf16-20261101` was stopped
after validation and retained in exited state for its startup log. GPU 2
returned to 18 MiB used. Existing services on GPUs 0 and 1 were not stopped or
reconfigured, and no paused batch was resumed.

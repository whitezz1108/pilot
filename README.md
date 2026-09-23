# pilot01 — omission propagation in a legal multi-agent workflow (Gate 2)

A deterministic, auditable orchestration skeleton for a research pilot on how an
**upstream omission error** propagates or gets corrected through a fixed
organizational workflow.

The conceptual workflow is strictly sequential:

```
frozen analyst memo  ->  Manager agent  ->  Compliance agent  ->  final decision
```

* **Gate 1** built the plumbing: schemas, state, restricted views, the runner,
  the event model, the export boundary. No model calls.
* **Gate 1.5** hardened it: no behavioural assumptions in the fixture agents, no
  policy categories in `src/`, and a safe persistence boundary.
* **Gate 2** (this gate) puts **real, model-backed Manager and Compliance agents**
  behind an explicit model-client boundary — with versioned prompts, structured
  output validation, one format-only repair, and an auditable raw-call log.

The prompts shipped here are **Gate-2 development prompts, not frozen
confirmatory prompts.** They have not been reviewed for the formal run.

## What Gate 2 does and does not include

| Included | Excluded (deliberately, and still) |
| --- | --- |
| A provider-neutral `ModelClient` boundary + one OpenAI-compatible adapter | Any vendor SDK (Anthropic / OpenAI / DeepSeek / GLM) |
| Real Manager and Compliance nodes driven by that boundary | BM25, embeddings, vector stores, RAG, `search_contract()` |
| Versioned prompt files (`prompts/*.md`) with runtime version ids | Tool calling, source-tool routing, A1V1 evidence enforcement |
| Structured parsing against the existing schemas, one format-only repair | Verifier behavioural logic |
| A JSONL raw model-call log, separate from `ExecutionRecord` | Gold-based scoring, experiment metrics, final-action scoring |
| CUAD downloaded with recorded provenance (`data/manifest.md`) | The 12-case development run, the 40-case pilot, batch execution |
| Two explicit registries: scripted doubles, or model-backed | LangGraph, workflow redesign, revision loops, `max_revision_rounds > 0` |
| A test suite that cannot reach the network | `A0V1`, databases, UI, web server, deployment, Study 2 memory |

The Analyst is conceptually an agent role, but during the formal experiment it is
**never called as a model**: each contract already has a frozen memo artifact.
`nodes/analyst.py` is a deterministic artifact loader. The frozen memo *is* the
experimental intervention, so loading it is not a model call.

## Experimental design

**Error conditions (hidden from all agents):**

* `E0` — the correct frozen memo.
* `E1` — the same memo with **exactly one** target policy-relevant claim
  deleted, produced offline by `schemas.build_omission_memo` (no model, no
  rewriting, no reordering, no marker).

Both arms share the same contract, the same `contract_text_hash`, the same memo
id and the same gold label: the omission changes the *memo*, not the contract.

**Governance conditions** (`config/conditions_v1.yaml`):

| id | `source_access` | `verification_required` |
| --- | --- | --- |
| `A0V0` | false | false |
| `A1V0` | true | false |
| `A1V1` | true | true |

`A0V1` (no source access but verification required) is **not** a main condition —
it is a degenerate cell and is rejected by the schema. It is recorded in the
config as an excluded condition, with its reason, so the exclusion is auditable
rather than merely absent.

**Policy under test** (`POLICY-01`, in `schemas.policy_01`): if either target
critical clause is present the case must be ESCALATE; only a confirmed *absent*
status permits ACCEPT, so `unknown` escalates. This is an experimental
organizational rule, not legal advice.

Which clause categories count as critical is **not** a property of the rule: it
is an audited design input. `policy_01(target_clause_categories=...)` takes it as
a mandatory keyword argument, `ExperimentalPolicy` refuses an empty or duplicated
list, and `ExperimentState` has no default policy, so a run cannot fall back to a
built-in category list. The synthetic placeholders used by the tests live in
`tests/sample_case.py`; a test asserts that no such category name appears anywhere
under `src/`.

## Architecture

```
src/pilot01/
├─ schemas.py            enums, memo, policy, agent outputs, hidden gold,
│                        deterministic omission construction
├─ config.py             typed loaders for the three versioned YAML files
├─ prompts.py            versioned prompt files: load, identify, render strictly
├─ events.py             append-only event model + JSONL writer
├─ model/                the provider boundary. Imports pydantic + stdlib only;
│  ├─ client.py          ModelParams/Request/Response, TokenUsage, ModelClient protocol
│  ├─ scripted.py        ScriptedModelClient: replays declared responses
│  ├─ parsing.py         JSON extraction, schema validation, the one repair
│  ├─ log.py             ModelCallRecord / ModelCallLog (JSONL) + secret refusal
│  └─ openai_compat.py   the ONLY module that knows about HTTP (injectable transport)
└─ workflow/
   ├─ state.py           ExperimentState (frozen treatment fields)
   ├─ views.py           restricted agent-facing inputs + leak audit
   ├─ export.py          safe persistence boundary: ExecutionRecord vs ScoringRecord
   ├─ transitions.py     transition kinds, Node, NodeRegistry, NodeResult
   ├─ runner.py          the sequential runner
   └─ nodes/
      ├─ analyst.py      deterministic artifact loader (not an agent)
      ├─ script.py       FakeAgentScript: fixture-declared outputs
      ├─ render.py       pure formatters: permitted data -> prompt text
      ├─ llm_agent.py    shared infrastructure for a model-backed node
      ├─ manager.py      FakeManager, and the real Manager
      └─ compliance.py   FakeCompliance + Verifier interface, and the real Compliance

config/    workflow_v1.yaml, conditions_v1.yaml, models_v1.yaml
prompts/   manager_v1.md, compliance_v1.md, repair_v1.md
data/      manifest.md (tracked); raw/ (ignored, immutable CUAD checkout)
scripts/   cuad_provenance.py (re-runnable provenance recorder)
```

The runner owns control flow; nodes own behaviour. A node returns a
`NodeResult` carrying a transition **intent**, and the runner resolves the
destination from the workflow's edge table. Nodes never choose their own
successor. **The runner is unchanged by Gate 2** — the model-backed agents
implement the same `Node` interface as the doubles.

### The Gate-2 runtime path

```
ExperimentState
  │
  ├─ node.build_input(state, invocation)        runner enforces RestrictedView
  │     └─ build_manager_view / build_compliance_view   (explicit whitelist)
  │
  ├─ assert_view_clean(view)                    runner audits the serialized view
  │
  ├─ node.run(view)                             ── the node is infrastructure only
  │     ├─ prompt.build_messages(view)          one pure function, one argument
  │     ├─ ModelRequest(messages, params, role, prompt_version, invocation, run_id)
  │     ├─ audit_keys(request)                  the request meets the view's standard
  │     ├─ call_structured(client, request, output_model, repair=...)
  │     │     ├─ client.generate(request)       the transport; scripted in tests
  │     │     ├─ extract_json_object(text)      fenced | bare | first balanced object
  │     │     ├─ output_model.model_validate    the EXISTING schema, not a copy
  │     │     └─ at most ONE format-only repair, both raw responses kept
  │     ├─ ModelCallLog.append(record)          required, so a call is never unlogged
  │     └─ NodeResult(output=<domain object>)
  │
  └─ node.apply(state, output)                  state.manager_output / compliance_output
```

There is no branch anywhere on that path that could set a `clause_status`, choose
a `decision`, or supply a missing field.

### Future feedback loop

`workflow_v1.yaml` is strictly sequential and sets `max_revision_rounds: 0`.
The extension

```
manager <- compliance      (transition kind: request_revision)
```

requires only a `request_revision` edge plus a non-zero budget in a later
`workflow_v*.yaml` — no engine change. The resolution logic already exists and is
**rejected, not absent**, while the loop is off: the config refuses to declare a
revision edge when the budget is 0, and the runner raises
`FeedbackLoopDisabledError` if a node requests one anyway. Both paths are tested,
including a test that drives the loop through the same engine with the route
enabled.

## The model-client boundary

`model/client.py` defines `ModelClient`: one method, `generate(request) ->
ModelResponse`. Everything above it — nodes, runner, state — types against that
protocol and never against an implementation. Everything below it — URLs,
headers, envelopes, retries, the credential — lives in
`model/openai_compat.py` and nowhere else.

* **No default client, anywhere.** There is no environment variable, no registry
  fallback and no lazy construction that could turn a deterministic test into a
  live call. Constructing `OpenAICompatibleClient` *is* the opt-in.
* **The transport is injectable.** `transport(prepared_request, timeout) ->
  bytes`. The default uses stdlib `urllib`; the tests substitute a fake, so the
  exact request that *would* be sent is asserted without sending it.
* **The credential lives on the adapter object and nowhere else.** It is read
  from the environment at construction, put into one header, and never copied
  into a `ModelRequest`, a `ModelResponse` or a `ModelCallRecord`. The adapter
  redacts it from every message it raises, and `assert_no_secrets` refuses to log
  a record containing a key-shaped string.
* **Parameters come from `config/models_v1.yaml`**, per role, so the model id,
  temperature and output cap that a run used are the ones written to the call
  log. Base URL and key come from `PILOT01_BASE_URL` / `PILOT01_API_KEY` and are
  never stored in a config file.

> **`model_id: gpt-4o-mini` in `config/models_v1.yaml` is a PLACEHOLDER.** It is
> a cheap OpenAI-compatible default so the wiring can be exercised end to end. It
> is not a chosen experimental model. Set it to the model this pilot is actually
> run on before collecting any data.

To run a live call (never from the test suite):

```python
from pilot01 import ModelCallLog, load_models_v1
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.workflow.nodes import build_llm_registry

models = load_models_v1()
manager = OpenAICompatibleClient()      # reads PILOT01_BASE_URL, PILOT01_API_KEY
compliance = OpenAICompatibleClient()   # a separate transport per role, on purpose
registry = build_llm_registry(
    repository=repository,
    models=models,
    manager_client=manager,
    compliance_client=compliance,
    call_log=ModelCallLog("data/runs/run-0001.jsonl"),
)
```

## Prompts

Substantive instructions live in version-controlled files, not in Python:
`prompts/manager_v1.md`, `prompts/compliance_v1.md`, `prompts/repair_v1.md`.
`pilot01/prompts.py` loads them, exposes a version id (`manager_v1`) that is
written into every call record, and renders placeholders in a single strict pass
so a substituted value cannot itself be re-expanded.

Each role prompt states only: the role, the permitted inputs, the task, the
experimental policy supplied at runtime, the output requirements, the requirement
not to fabricate evidence that is not available, and the requirement to return
the structured schema. They do **not** mention the treatment arms, the omission,
hidden gold, the expected result, the research hypotheses, whether a case is
supposed to fail, or where the answer lives. Tests scan the prompt files and the
`src/` tree for those.

## Structured output and the single repair

`model/parsing.py` extracts a JSON object from the raw response (fenced block,
bare object, or the first balanced object in surrounding prose), validates it
against the **existing** schema — no schema is duplicated — and returns the
domain object.

If the first response is not valid JSON, or does not validate, **at most one**
repair attempt is made (`MAX_FORMAT_REPAIRS = 1`). The repair turn is *format
only*: it receives the model's own previous response and the validation error,
and nothing else. `RepairSpec.build_messages(previous_response, parse_error)`
has exactly that signature, so the view cannot be passed to it even by mistake.
It cannot add evidence, reveal gold, state the expected answer, or re-run
substantive reasoning with new context. Both the original and the repaired
response are preserved in the log.

If the repair also fails, the node raises `ProtocolError`, the runner marks the
run `FAILED`, and a `protocol_error` event is recorded. No field is ever invented
in Python to fill a gap.

## The raw model-call log

`model/log.py` writes one JSONL record per provider call to `data/runs/<run>.jsonl`
(ignored by git). Each record carries the run identifier, role, invocation,
prompt version, provider, exact model id, model parameters, the rendered
permitted input, request and response timestamps, latency, the raw response text,
the parsed output or the parse error, the repair attempt and its reason if any,
token usage, the provider request id, and error information.

* `ExecutionRecord` is **not** reused for this. The public record keeps its
  export-safe guarantee; the raw log is a local run artefact. A test asserts the
  two share exactly one field name (`run_id`) and that no raw-call record is ever
  written into an execution record.
* **No API key or Authorization header is ever logged**, and `assert_no_secrets`
  refuses the write rather than trusting the caller.
* **No hidden gold is ever written to a model-call record.** The log has no field
  for it, and the records are built from the view, not from the state.

## CUAD provenance

`scripts/cuad_provenance.py` clones `https://github.com/The-Atticus-Project/cuad`
(shallow), records the exact upstream commit, extracts `data.zip`, locates the
canonical annotation file **by name** rather than assuming an archive layout, and
writes `data/manifest.md`: source URL, upstream commit, retrieval date, clone
depth, archive path, extracted path, canonical file path, CUAD internal version,
SHA-256 of the canonical file, and the contract count.

Upstream names the archive member `CUADv1.json`; the underscore spelling
`CUAD_v1.json` does not appear in the archive, so the lookup is case- and
separator-tolerant and the discrepancy is recorded in the manifest.

`data/raw/` is treated as **immutable source material** and is excluded from
version control. Nothing under it is modified. `data/` as a whole is *not*
ignored, because later Gates add cases, registries and result manifests that must
be tracked; tests assert both halves of that rule.

## Experimental-validity protections

1. **Hidden data cannot enter a view.** `ManagerInput` and `ComplianceInput` are
   frozen, closed models built by explicit field-by-field whitelists.
   `RestrictedView` refuses *at class-definition time* to declare a field whose
   name looks like hidden data, so a future edit cannot add `gold_action` to a
   view and have it silently work.
2. **Runtime audit, now on the request too.** The runner calls `assert_view_clean`
   on every restricted view before dispatch; a model-backed node additionally
   calls `audit_keys` on the serialized `ModelRequest` before sending it, so the
   request is held to the same standard as the view it was rendered from.
3. **Role isolation.** Compliance never receives the analyst memo, the Manager's
   execution history, prior message history, or any treatment label — only the
   Manager handoff. `build_compliance_view` never reads the memo at all, so there
   is no code path by which it could reach Compliance. The real Compliance node
   inherits this unchanged: it renders from `ComplianceInput` and nothing else.
4. **Fresh context per agent call.** Each call builds a new frozen view carrying
   its `invocation` index and renders a fresh message list. No message-history
   object exists anywhere in the codebase, no assistant turn is ever sent back,
   and the two roles use separate transports. Tests assert byte-identical
   messages across repeated runs.
5. **Treatment integrity.** `ExperimentState` freezes every identity, treatment
   and case-metadata field, so the orchestration layer cannot mutate a condition
   mid-run. `check_treatment_integrity` re-derives the governance flags from the
   condition config and cross-checks the memo against the hidden omission record
   and the gold label; the runner runs it before the first node and records a
   `protocol_error` if it fails.
6. **Loud failure.** A structural violation aborts the run and is logged. Nothing
   is smoothed over, because silently continuing would corrupt the observation.
7. **No behavioural assumptions anywhere in `src/`.** The fixture agents replay
   outputs a test declared (`FakeAgentScript`) and infer nothing. The real agents
   are infrastructure: view → render → call → parse → return. Neither contains a
   rule such as "no target claim in the memo means the clause is absent".
   Whether an omitted claim is read downstream as `absent`, as `unknown`, or is
   recovered as `present`, and whether an agent that *may* consult sources
   actually does, are the empirical outcomes the pilot measures. Encoding any of
   them in `src/` would manufacture the effect and disguise it as a property of
   the workflow. Guard-style tests parse the node modules and fail if any of them
   so much as names a gold or treatment value.
8. **A safe export boundary.** `ExperimentState` and `RunOutcome` legitimately
   carry gold, so neither is an export format. `workflow/export.py` defines the
   public `ExecutionRecord` (run identity, researcher-facing condition and case
   metadata, node outputs, event references, execution metadata — no gold) built
   by explicit whitelist, with a validator that walks its own serialization and
   refuses any key that looks like gold or omission data. Hidden labels leave a
   run only through `build_scoring_record`, an explicitly separate scorer-only
   path.
9. **The suite cannot reach the network.** An autouse fixture makes any outbound
   connection an `AssertionError`; a test drives the real adapter with its real
   transport and asserts it still cannot connect. Statically, exactly one module
   may import a network library, no workflow module may name that module, no
   vendor SDK may be imported at all, only the adapter may read a credential from
   the environment, and no tracked file may contain a key-shaped string outside
   the two files that test redaction.
10. **No implicit default scenario, and no implicit default client.** Both
    registries require every collaborator by name. `build_registry` needs both
    fake scripts; `build_llm_registry` needs both transports and a call log. A
    default scenario would be a behavioural assumption wearing a convenient hat.

## Running the tests

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # or: pip install pydantic PyYAML pytest
.venv/Scripts/python.exe -m pytest
```

Dependencies: `pydantic`, `PyYAML`, `pytest`. Nothing else — the provider
adapter uses the standard library.

The suite runs **offline**. It never places a paid call and needs no API key.
Tests that need the CUAD download skip themselves when `data/raw/` is absent.

## Still to come (Gate 2.5 and beyond)

Deliberately unimplemented here:

* **The dataset audit.** The target clause categories in `tests/sample_case.py`
  are synthetic placeholders. Selecting the real CUAD categories is a separate
  audited step, and the prompts must be re-reviewed afterwards.
* **Prompt review and freezing.** `manager_v1.md` and `compliance_v1.md` are
  Gate-2 development prompts. They are not confirmatory instruments yet.
* **The model id.** `gpt-4o-mini` is a placeholder.
* **Retrieval.** BM25, embeddings, a vector store, `search_contract()`,
  `open_source_span()`, paragraphization and source-tool routing — so
  `source_access: true` currently changes nothing about what an agent can do.
* **A1V1 evidence enforcement**, and the `Verifier` / `VerificationOutcome`
  behaviour. They are declared and deliberately unwired: any stub would fix an
  experimental cell before the tool layer exists.
* **Scoring.** Gold-based scoring, experiment metrics, automated final-action
  scoring, bootstrap statistics, plots.
* **Execution at scale.** The 12-case development run, the 40-case pilot, batch
  running, revision loops (`max_revision_rounds > 0`), `A0V1`, Study 2
  organizational memory, and any database, UI or deployment layer.

## Gate-1/1.5 placeholders that remain

* **`Verifier` / `VerificationOutcome`** — declared, unwired, and still so.
* **Confidence values** in the test scenarios are arbitrary, not calibrated.
* **Clause categories** in `tests/sample_case.py` are synthetic placeholders, to
  be replaced after the dataset audit. Nothing in `src/` names a category.
* **Gold labels are not written to the event log** (they stay in the state and are
  joined at scoring time), so the log can be shared without disclosing the answer.
  The treatment labels *are* logged, because the log must be interpretable — and
  they *are* in the public `ExecutionRecord`, which is researcher-facing. Agent
  views remain free of both.
* **`RunOutcome.model_dump()` is not an export.** Use
  `RunOutcome.to_execution_record()`.

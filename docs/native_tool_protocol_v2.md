# Native tool protocol v2

`native-v2` is a new experiment/runtime version.  It does not alter the
frozen `native-v1` replay path.

## State machine

1. A node begins with `search_contract` only.  `open_source_span` appears only
   after the current invocation's search has returned at least one paragraph;
   its `paragraph_id` schema enumerates exactly those returned IDs.  The tool
   boundary independently checks the same rule.
2. One assistant response may contain exactly one native tool call.  Provider
   requests carry `parallel_tool_calls: false`; the workflow independently
   rejects a batch, a tool not offered on that turn, or values outside the
   turn's closed schema before dispatching anything.
3. A search with another page creates an opaque cursor.  Only then does the
   next request expose `next_search_page`, with an enum containing only the
   currently valid cursor values.
4. A v2 cursor is consumed by its continuation call and cannot be reused.  If
   no cursor remains, pagination disappears from the next tool surface.
5. After the sixteenth executed call, the next request exposes no tools and
   asks for a final answer.  A remaining budget of one still permits one call,
   never a two-call batch.

Tool-call refusal or a schema violation is a protocol failure, not an
unlogged recovery path.  This deliberately makes the previous
`invalid_check_only` cursor and last-budget two-call batch impossible to
silently proceed through.

## Retrieval

The paired `human-lexical-v2` ranker remains fully contract-local and
deterministic.  It starts from BM25, suppresses a small fixed list of function
words when other terms exist, then adds transparent bonuses for query-term
coverage, exact phrase occurrence, and close co-occurrence.  Result payloads
record the matched terms, coverage, phrase flag, and proximity window so a
rank can be audited.  It does not use embeddings, an external search service,
or a classifier that decides whether the contract contains a target.

Keyword scoring and phrase matching use different query representations.
Keyword terms remove configured function words and then deduplicate; phrase
tokens preserve order, repetition, and internal function words, while trimming
configured function words only from the two ends.  Phrase matching is a
ranking bonus, never a mandatory filter, and scores are query-local ranking
signals rather than probabilities or cross-query measures.

Search results contain verbatim source excerpts.  V2 excerpt selection first
centres an exact ordered phrase match when one exists; otherwise it selects the
earliest window covering the greatest number of distinct effective keyword
terms.  The result and audit locator record the excerpt's absolute contract
offsets.  A returned excerpt establishes partial source exposure; only an
`open_source_span` event establishes the protocol's required full-paragraph
reading, and neither event by itself establishes semantic understanding.

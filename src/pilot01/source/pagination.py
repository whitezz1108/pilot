"""Contract-local BM25 paging with opaque per-invocation cursors."""

from __future__ import annotations

import secrets

from ..model.tool_protocol import ToolDefinition
from .index import excerpt, tokenize
from .tools import (
    OPEN_SOURCE_SPAN,
    SEARCH_CONTRACT,
    SearchHit,
    SourceToolError,
    SourceTools,
    ToolOutcome,
    timed_tool,
)

NEXT_SEARCH_PAGE = "next_search_page"
PAGED_SOURCE_TOOL_NAMES = (SEARCH_CONTRACT, NEXT_SEARCH_PAGE, OPEN_SOURCE_SPAN)


class PagedSourceTools(SourceTools):
    """Paged source access in legacy or native-v1 protocol form.

    Legacy protocol 2 keeps the historical combined
    ``search_contract(query, target_category, cursor?)`` shape for frozen runs.
    Native-v1 makes continuation a separate ``next_search_page(cursor)`` tool,
    so a model cannot pair a cursor with a different query or target.
    """

    def __init__(self, *, target_categories, protocol_version="2", **kwargs):
        super().__init__(**kwargs)
        if protocol_version not in {"2", "native-v1"}:
            raise ValueError("paged source protocol must be '2' or 'native-v1'")
        self.protocol_version = protocol_version
        self.target_categories = tuple(target_categories)
        self._reset_pages()

    @property
    def tool_names(self):
        return (
            PAGED_SOURCE_TOOL_NAMES
            if self.protocol_version == "native-v1"
            else super().tool_names
        )

    @property
    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        if self.protocol_version != "native-v1":
            return super().tool_definitions
        target_schema = {"type": "string", "enum": list(self.target_categories)}
        return (
            ToolDefinition(
                name=SEARCH_CONTRACT,
                description=(
                    "Start a search of the current contract for one policy target. "
                    "Returns ranked snippets and an opaque next_cursor when another page exists."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "target_category": target_schema,
                    },
                    "required": ["query", "target_category"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name=NEXT_SEARCH_PAGE,
                description=(
                    "Continue the exact search bound to an opaque cursor. "
                    "Do not supply or change the query or target category."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"cursor": {"type": "string", "minLength": 1}},
                    "required": ["cursor"],
                    "additionalProperties": False,
                },
            ),
            super().tool_definitions[1],
        )

    def _reset_pages(self):
        self._rankings = {}
        self._cursors = {}
        self._tokens = {}
        self._first_seen = set()
        self._returned = set()

    def begin_invocation(self, **kwargs):
        super().begin_invocation(**kwargs)
        self._reset_pages()

    def _validate_query(self, query, target):
        if not isinstance(query, str) or not query.strip():
            raise SourceToolError("query must be a nonempty string")
        if not isinstance(target, str) or target not in self.target_categories:
            raise SourceToolError("target_category must name exactly one policy target")
        return query.strip(), target

    def _cursor_position(self, cursor):
        if not isinstance(cursor, str) or cursor not in self._cursors:
            raise SourceToolError("cursor is unknown or expired in this node invocation")
        return self._cursors[cursor]

    def _page(self, *, tool, arguments, key, offset, repeat_first=False):
        document, index = self._require_source()
        query, target = key
        if key not in self._rankings:
            self._rankings[key] = index.search(query, top_k=len(index))
        ranked = self._rankings[key]
        selected = ranked[offset : offset + self._top_k]
        end = offset + len(selected)
        has_more = end < len(ranked)
        next_cursor = None
        if has_more:
            cursor_key = (key, end)
            if cursor_key not in self._tokens:
                token = secrets.token_hex(16)
                self._tokens[cursor_key] = token
                self._cursors[token] = (key, end)
            next_cursor = self._tokens[cursor_key]
        hits = (
            tuple(
                SearchHit(
                    paragraph_id=paragraph.paragraph_id,
                    contract_id=document.contract_id,
                    score=round(score, 6),
                    excerpt=excerpt(paragraph.text, tokenize(query), self._excerpt_chars),
                )
                for paragraph, score in selected
            )
            if not repeat_first
            else ()
        )
        ids = tuple(hit.paragraph_id for hit in hits)
        already = tuple(pid for pid in ids if pid in self._returned)
        self._returned.update(ids)
        self._first_seen.add(key)
        self.ledger.record_search(query, ids, target_category=target, offset=offset)
        payload = {
            "results": [
                dict(hit.model_dump(mode="json"), rank=offset + rank + 1)
                for rank, hit in enumerate(hits)
            ],
            "offset": offset,
            "range_start": offset + 1 if selected else None,
            "range_end": end,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "repeat_page": repeat_first,
            "already_seen_ids": list(already),
            "new_result_count": len(ids) - len(already),
            "total_matching": len(ranked),
            "target_category": target,
            "query": query,
        }
        if repeat_first:
            payload["notice"] = (
                "First page already provided; use next_cursor to continue. "
                "No new passages returned."
            )
        return self._emit(
            ToolOutcome(
                tool=tool,
                arguments=arguments,
                ok=True,
                result_ids=ids,
                payload=payload,
            )
        ), hits

    @timed_tool
    def _invoke(self, tool, arguments):
        if tool == SEARCH_CONTRACT:
            try:
                if self.protocol_version == "native-v1":
                    if set(arguments) != {"query", "target_category"}:
                        raise SourceToolError(
                            "search_contract requires exactly query and target_category; "
                            "continue with next_search_page(cursor)"
                        )
                    query, target = self._validate_query(
                        arguments["query"], arguments["target_category"]
                    )
                    key = (query, target)
                    return self._page(
                        tool=tool,
                        arguments=arguments,
                        key=key,
                        offset=0,
                        repeat_first=key in self._first_seen,
                    )

                if set(arguments) - {"query", "target_category", "cursor"} or not {
                    "query",
                    "target_category",
                } <= set(arguments):
                    raise SourceToolError(
                        "search_contract requires query and target_category; cursor is optional"
                    )
                query, target = self._validate_query(
                    arguments["query"], arguments["target_category"]
                )
                key = (query, target)
                cursor = arguments.get("cursor")
                offset = 0
                if cursor is not None:
                    cursor_key, offset = self._cursor_position(cursor)
                    if cursor_key != key:
                        raise SourceToolError("cursor query or target_category mismatch")
                return self._page(
                    tool=tool,
                    arguments=arguments,
                    key=key,
                    offset=offset,
                    repeat_first=cursor is None and key in self._first_seen,
                )
            except SourceToolError as exc:
                return self._emit(
                    ToolOutcome(tool=tool, arguments=arguments, ok=False, error=str(exc))
                ), None

        if tool == NEXT_SEARCH_PAGE and self.protocol_version == "native-v1":
            try:
                if set(arguments) != {"cursor"}:
                    raise SourceToolError("next_search_page requires exactly cursor")
                key, offset = self._cursor_position(arguments["cursor"])
                return self._page(
                    tool=tool,
                    arguments=arguments,
                    key=key,
                    offset=offset,
                )
            except SourceToolError as exc:
                return self._emit(
                    ToolOutcome(tool=tool, arguments=arguments, ok=False, error=str(exc))
                ), None

        return super()._invoke(tool, arguments)

    def search_contract(self, query, *, target_category, cursor=None):
        args = {"query": query, "target_category": target_category}
        if cursor is not None:
            if self.protocol_version == "native-v1":
                raise SourceToolError(
                    "native-v1 continues searches with next_search_page(cursor)"
                )
            args["cursor"] = cursor
        outcome, _ = self._invoke(SEARCH_CONTRACT, args)
        if not outcome.ok:
            raise SourceToolError(outcome.error)
        return outcome

    def next_search_page(self, cursor):
        if self.protocol_version != "native-v1":
            raise SourceToolError("next_search_page is available only in native-v1")
        outcome, _ = self._invoke(NEXT_SEARCH_PAGE, {"cursor": cursor})
        if not outcome.ok:
            raise SourceToolError(outcome.error)
        return outcome

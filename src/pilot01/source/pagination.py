"""Contract-local BM25 paging with opaque per-invocation cursors."""

from __future__ import annotations

import secrets

from ..model.tool_protocol import ToolDefinition
from .index import excerpt, select_lexical_excerpt, tokenize
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
    """Paged source access in legacy, native-v1, or strict native-v2 form.

    Legacy protocol 2 keeps the historical combined
    ``search_contract(query, target_category, cursor?)`` shape for frozen runs.
    Native-v1/v2 make continuation a separate ``next_search_page(cursor)`` tool,
    so a model cannot pair a cursor with a different query or target.
    """

    def __init__(self, *, target_categories, protocol_version="2",
                 reject_duplicate_searches=False, max_search_pages_per_query=None,
                 reject_duplicate_opens=False, **kwargs):
        super().__init__(**kwargs)
        if protocol_version not in {"2", "native-v1", "native-v2"}:
            raise ValueError("paged source protocol must be '2', 'native-v1', or 'native-v2'")
        self.protocol_version = protocol_version
        self.target_categories = tuple(target_categories)
        self.reject_duplicate_searches = bool(reject_duplicate_searches)
        self.max_search_pages_per_query = max_search_pages_per_query
        self.reject_duplicate_opens = bool(reject_duplicate_opens)
        if (max_search_pages_per_query is not None
                and (type(max_search_pages_per_query) is not int
                     or max_search_pages_per_query < 1)):
            raise ValueError("max_search_pages_per_query must be a positive integer")
        self._reset_pages()

    @property
    def tool_names(self):
        return (
            PAGED_SOURCE_TOOL_NAMES
            if self.protocol_version in {"native-v1", "native-v2"}
            else super().tool_names
        )

    @property
    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        if self.protocol_version not in {"native-v1", "native-v2"}:
            return super().tool_definitions
        target_schema = {"type": "string", "enum": list(self.target_categories)}
        search_description = (
            "Start a broad lexical search of the current contract for one policy target. "
            "Returns ranked verbatim excerpts and an opaque next_cursor when another page exists. "
            "Exact ordered query phrases receive a ranking bonus but are not mandatory filters; "
            "quotation marks have no special matching semantics, and score is not a probability."
            if self.protocol_version == "native-v2"
            else "Start a search of the current contract for one policy target. Returns ranked snippets and an opaque next_cursor when another page exists."
        )
        open_definition = super().tool_definitions[1]
        if self.protocol_version == "native-v2" and self._returned:
            open_definition = ToolDefinition(
                name=OPEN_SOURCE_SPAN,
                description=(
                    "Open an exact paragraph returned by search in this node invocation. "
                    "Only the currently enumerated paragraph ids may be opened."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "paragraph_id": {"type": "string", "enum": sorted(self._returned)}
                    },
                    "required": ["paragraph_id"],
                    "additionalProperties": False,
                },
            )
        return (
            ToolDefinition(
                name=SEARCH_CONTRACT,
                description=search_description,
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
            *(
                (
                    ToolDefinition(
                        name=NEXT_SEARCH_PAGE,
                        description=(
                            "Continue the exact search bound to an opaque cursor. "
                            "Do not supply or change the query or target category."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "cursor": (
                                    {"type": "string", "enum": sorted(self._cursors)}
                                    if self.protocol_version == "native-v2"
                                    else {"type": "string", "minLength": 1}
                                )
                            },
                            "required": ["cursor"],
                            "additionalProperties": False,
                        },
                    ),
                )
                if self.protocol_version == "native-v1" or self._cursors
                else ()
            ),
            *(
                (open_definition,)
                if self.protocol_version == "native-v1" or self._returned
                else ()
            ),
        )

    def _reset_pages(self):
        self._rankings = {}
        self._cursors = {}
        self._tokens = {}
        self._first_seen = set()
        self._returned = set()
        self._normalized_queries = set()

    def begin_invocation(self, **kwargs):
        super().begin_invocation(**kwargs)
        self._reset_pages()

    def _validate_query(self, query, target):
        if not isinstance(query, str) or not query.strip():
            raise SourceToolError("query must be a nonempty string")
        if not isinstance(target, str) or target not in self.target_categories:
            raise SourceToolError("target_category must name exactly one policy target")
        return query.strip(), target

    def _cursor_position(self, cursor, *, consume=False):
        if not isinstance(cursor, str) or cursor not in self._cursors:
            raise SourceToolError("cursor is unknown or expired in this node invocation")
        position = self._cursors[cursor]
        if consume:
            del self._cursors[cursor]
        return position

    def _page(self, *, tool, arguments, key, offset, repeat_first=False):
        document, index = self._require_source()
        query, target = key
        if key not in self._rankings:
            self._rankings[key] = index.search(query, top_k=len(index))
        ranked = self._rankings[key]
        selected = ranked[offset : offset + self._top_k]
        end = offset + len(selected)
        source_has_more = end < len(ranked)
        page_number = offset // self._top_k + 1
        page_limit_reached = bool(
            source_has_more
            and self.max_search_pages_per_query is not None
            and page_number >= self.max_search_pages_per_query
        )
        has_more = source_has_more and not page_limit_reached
        next_cursor = None
        if has_more:
            cursor_key = (key, end)
            if cursor_key not in self._tokens or self._tokens[cursor_key] not in self._cursors:
                token = secrets.token_hex(16)
                self._tokens[cursor_key] = token
                self._cursors[token] = (key, end)
            next_cursor = self._tokens[cursor_key]
        def make_hit(paragraph, score):
            extra = {}
            rendered_excerpt = excerpt(paragraph.text, tokenize(query), self._excerpt_chars)
            if index.ranking_version == "human-lexical-v2":
                rendered_excerpt, relative_start, relative_end, selection = select_lexical_excerpt(
                    paragraph.text, query, self._excerpt_chars
                )
                extra = dict(
                    **index.diagnostics(paragraph, query),
                    excerpt_start_char=paragraph.start_char + relative_start,
                    excerpt_end_char=paragraph.start_char + relative_end,
                    excerpt_selection=selection,
                )
            return SearchHit(
                    paragraph_id=paragraph.paragraph_id,
                    contract_id=document.contract_id,
                    score=round(score, 6),
                    excerpt=rendered_excerpt,
                    **extra,
                )
        hits = (
            tuple(
                make_hit(paragraph, score)
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
        if self.max_search_pages_per_query is not None:
            payload.update(
                source_has_more=source_has_more,
                page_number=page_number,
                page_limit_reached=page_limit_reached,
                max_pages_per_query=self.max_search_pages_per_query,
            )
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
                if self.protocol_version in {"native-v1", "native-v2"}:
                    if set(arguments) != {"query", "target_category"}:
                        raise SourceToolError(
                            "search_contract requires exactly query and target_category; "
                            "continue with next_search_page(cursor)"
                        )
                    query, target = self._validate_query(
                        arguments["query"], arguments["target_category"]
                    )
                    normalized = (tuple(tokenize(query)), target)
                    if self.reject_duplicate_searches and normalized in self._normalized_queries:
                        raise SourceToolError(
                            "duplicate normalized search query; use existing candidates, "
                            "continue with the offered cursor, or reformulate"
                        )
                    self._normalized_queries.add(normalized)
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

        if tool == NEXT_SEARCH_PAGE and self.protocol_version in {"native-v1", "native-v2"}:
            try:
                if set(arguments) != {"cursor"}:
                    raise SourceToolError("next_search_page requires exactly cursor")
                key, offset = self._cursor_position(
                    arguments["cursor"], consume=self.protocol_version == "native-v2"
                )
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
            if self.protocol_version in {"native-v1", "native-v2"}:
                raise SourceToolError(
                    "native-v1 continues searches with next_search_page(cursor)"
                )
            args["cursor"] = cursor
        outcome, _ = self._invoke(SEARCH_CONTRACT, args)
        if not outcome.ok:
            raise SourceToolError(outcome.error)
        return outcome

    def next_search_page(self, cursor):
        if self.protocol_version not in {"native-v1", "native-v2"}:
            raise SourceToolError("next_search_page is available only in native protocols")
        outcome, _ = self._invoke(NEXT_SEARCH_PAGE, {"cursor": cursor})
        if not outcome.ok:
            raise SourceToolError(outcome.error)
        return outcome

    def _open(self, paragraph_id):
        """Native-v2 opens only IDs returned by this node invocation's search."""
        if self.protocol_version == "native-v2" and isinstance(paragraph_id, str):
            candidate = paragraph_id.strip()
            document, _ = self._require_source()
            if document.paragraph(candidate) is not None and candidate not in self._returned:
                raise SourceToolError(
                    f"{candidate!r} was not returned by search in this node invocation; "
                    "search it before opening it"
                )
            if self.reject_duplicate_opens and candidate in self.ledger.opened_ids:
                raise SourceToolError(
                    f"{candidate!r} was already opened in this node invocation; "
                    "reuse the exact paragraph text already returned"
                )
        return super()._open(paragraph_id)

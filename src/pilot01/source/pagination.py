"""Contract-local BM25 paging with opaque per-invocation cursors."""
from __future__ import annotations

import secrets

from .index import excerpt, tokenize
from .tools import SourceTools, SourceToolError, ToolOutcome, SearchHit, SEARCH_CONTRACT, timed_tool


class PagedSourceTools(SourceTools):
    protocol_version = "2"

    def __init__(self, *, target_categories, **kwargs):
        super().__init__(**kwargs)
        self.target_categories = tuple(target_categories)
        self._reset_pages()

    def _reset_pages(self):
        self._rankings = {}
        self._cursors = {}
        self._tokens = {}
        self._first_seen = set()
        self._returned = set()

    def begin_invocation(self, **kwargs):
        super().begin_invocation(**kwargs)
        self._reset_pages()

    @timed_tool
    def _invoke(self, tool, arguments):
        if tool != SEARCH_CONTRACT:
            return super()._invoke(tool, arguments)
        try:
            document, index = self._require_source()
            if set(arguments) - {"query", "target_category", "cursor"} or not {"query", "target_category"} <= set(arguments):
                raise SourceToolError("search_contract requires query and target_category; cursor is optional")
            query, target, cursor = arguments["query"], arguments["target_category"], arguments.get("cursor")
            if not isinstance(query, str) or not query.strip():
                raise SourceToolError("query must be a nonempty string")
            if not isinstance(target, str) or target not in self.target_categories:
                raise SourceToolError("target_category must name exactly one policy target")
            query = query.strip()
            key = (query, target)
            offset = 0
            if cursor is not None:
                if not isinstance(cursor, str) or cursor not in self._cursors:
                    raise SourceToolError("cursor is unknown or expired in this node invocation")
                cursor_key, offset = self._cursors[cursor]
                if cursor_key != key:
                    raise SourceToolError("cursor query or target_category mismatch")
            if key not in self._rankings:
                self._rankings[key] = index.search(query, top_k=len(index))
            ranked = self._rankings[key]
            selected = ranked[offset:offset + self._top_k]
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
            repeat = cursor is None and key in self._first_seen
            hits = tuple(SearchHit(paragraph_id=p.paragraph_id, contract_id=document.contract_id,
                                   score=round(score, 6), excerpt=excerpt(p.text, tokenize(query), self._excerpt_chars))
                         for p, score in selected) if not repeat else ()
            ids = tuple(h.paragraph_id for h in hits)
            already = tuple(pid for pid in ids if pid in self._returned)
            self._returned.update(ids)
            self._first_seen.add(key)
            self.ledger.record_search(query, ids, target_category=target, offset=offset)
            payload = {"results": [dict(h.model_dump(mode="json"), rank=offset + i + 1) for i, h in enumerate(hits)],
                       "offset": offset, "range_start": offset + 1 if selected else None, "range_end": end,
                       "has_more": has_more, "next_cursor": next_cursor, "repeat_page": repeat,
                       "already_seen_ids": list(already), "new_result_count": len(ids) - len(already),
                       "total_matching": len(ranked), "target_category": target}
            payload["query"] = query
            if repeat:
                payload["notice"] = "First page already provided; use next_cursor to continue. No new passages returned."
            return self._emit(ToolOutcome(tool=tool, arguments=arguments, ok=True, result_ids=ids, payload=payload)), hits
        except SourceToolError as exc:
            return self._emit(ToolOutcome(tool=tool, arguments=arguments, ok=False, error=str(exc))), None

    def search_contract(self, query, *, target_category, cursor=None):
        args = {"query": query, "target_category": target_category}
        if cursor is not None:
            args["cursor"] = cursor
        outcome, _ = self._invoke(SEARCH_CONTRACT, args)
        if not outcome.ok:
            raise SourceToolError(outcome.error)
        return outcome

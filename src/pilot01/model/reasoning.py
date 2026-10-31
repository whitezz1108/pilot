"""Reasoning observations, never workflow input or an estimate of GPU compute time."""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path


class StreamCapture:
    """Reassemble one choice while retaining timestamped provider SSE events."""

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.started = clock()
        self.events = []
        self.content = []
        self.reasoning = []
        self.tool_calls = {}
        self.envelope = {"choices": [{"index": 0, "message": {"content": ""}, "finish_reason": None}]}
        self.first_reasoning = self.last_reasoning = self.first_content = None
        self.done = False

    def feed(self, data):
        elapsed = self.clock() - self.started
        if data == "[DONE]":
            self.done = True
            return
        event = json.loads(data)
        self.events.append({"elapsed_seconds": elapsed, "data": event})
        if event.get("error"):
            raise ValueError("provider stream error")
        for key in ("id", "model", "usage"):
            if event.get(key) is not None:
                self.envelope[key] = event[key]
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            for field, sink in (("reasoning_content", self.reasoning), ("content", self.content)):
                value = delta.get(field)
                if isinstance(value, str) and value:
                    sink.append(value)
                    if field == "reasoning_content":
                        if self.first_reasoning is None:
                            self.first_reasoning = elapsed
                        self.last_reasoning = elapsed
                    elif self.first_content is None:
                        self.first_content = elapsed
            for tool_delta in delta.get("tool_calls") or []:
                if not isinstance(tool_delta, dict):
                    continue
                index = tool_delta.get("index", 0)
                call = self.tool_calls.setdefault(
                    index,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_delta.get("id") is not None:
                    call["id"] = tool_delta["id"]
                function = tool_delta.get("function") or {}
                if isinstance(function.get("name"), str):
                    call["function"]["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    call["function"]["arguments"] += function["arguments"]
                if self.first_content is None:
                    self.first_content = elapsed
            if choice.get("finish_reason") is not None:
                self.envelope["choices"][0]["finish_reason"] = choice["finish_reason"]

    def snapshot(self):
        message = self.envelope["choices"][0]["message"]
        message["content"] = "".join(self.content)
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tool_calls:
            message["content"] = None
            message["tool_calls"] = [self.tool_calls[index] for index in sorted(self.tool_calls)]
        return self.envelope

    def timing(self):
        first, last, answer = self.first_reasoning, self.last_reasoning, self.first_content
        ordered = first is not None and answer is not None and last <= answer
        return {
            "timing_source": "client_stream_arrival",
            "stream_complete": self.done,
            "first_reasoning_seconds": first,
            "last_reasoning_seconds": last,
            "first_content_seconds": answer,
            "reasoning_span_seconds": last - first if first is not None else None,
            "reasoning_phase_seconds": answer - first if ordered else None,
            "reasoning_phase_status": "observed" if ordered else "boundary_unavailable",
            "timing_note": "Arrival intervals include buffering/network; exclude prefill before first reasoning. Not GPU compute time.",
        }


@lru_cache(maxsize=4)
def _tokenizer(path, size, mtime_ns):
    from tokenizers import Tokenizer
    data = Path(path).read_bytes()
    return Tokenizer.from_str(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def observe_reasoning(envelope, *, timing=None, tokenizer_path=None):
    choices = envelope.get("choices") or [{}]
    message = choices[0].get("message") or {}
    content = message.get("reasoning_content")
    source = "message.reasoning_content"
    if not isinstance(content, str):
        content = message.get("reasoning")
        source = "message.reasoning"
    if not isinstance(content, str):
        content, source = None, None
    usage = envelope.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    tokens = details.get("reasoning_tokens")
    token_source = "usage.completion_tokens_details.reasoning_tokens"
    if tokens is None:
        tokens = usage.get("reasoning_tokens")
        token_source = "usage.reasoning_tokens"
    if tokens is None:
        token_source = None
    info = dict(content_source=source, content_status="recorded" if content is not None else "not_returned",
                tokens=tokens, token_source=token_source, tokenizer_sha256=None,
                timing_source="unavailable_nonstream", reasoning_phase_seconds=None,
                reasoning_phase_status="unavailable_nonstream")
    if timing:
        info.update(timing)
    # Re-encoding text is explicitly separate from provider generation accounting.
    if tokens is None and content is not None and tokenizer_path:
        try:
            path = Path(tokenizer_path).resolve()
            stat = path.stat()
            tokenizer, sha = _tokenizer(str(path), stat.st_size, stat.st_mtime_ns)
            info.update(tokens=len(tokenizer.encode(content, add_special_tokens=False).ids),
                        token_source="local_tokenizer_reencode", tokenizer_sha256=sha,
                        tokenizer_path=str(path))
        except Exception as exc:
            info["token_count_error"] = type(exc).__name__
    return content, info

"""Explicit live execution settings, independent of model/treatment parameters.

No automatic retries of model answers, validator failures, timeouts or broken
connections. Only explicit HTTP 429/502/503/504 responses are retryable. All
HTTP attempts, including exhausted ones, leave a separate transport audit.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import ModelClientError
from .openai_compat import OpenAICompatibleClient, _urlopen_transport
from .tool_protocol import ToolTransport


class EndpointSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slot: int = Field(ge=1, le=32)
    max_in_flight: int = Field(ge=1, le=128)
    quota_group: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    reasoning_stream: bool = Field(default=False, exclude_if=lambda v: not v)
    reasoning_tokenizer_path: str | None = Field(default=None, exclude_if=lambda v: v is None)
    tool_transport: ToolTransport = "legacy-text"
    context_window_tokens: int | None = Field(default=None, ge=1, exclude_if=lambda v: v is None)
    context_safety_tokens: int = Field(default=0, ge=0, exclude_if=lambda v: v == 0)
    minimum_output_tokens: int = Field(default=1, ge=1, exclude_if=lambda v: v == 1)
    capacity_preflight: Literal["sglang-v1"] | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def context_budget(self):
        if self.capacity_preflight is not None and self.context_window_tokens is None:
            raise ValueError("capacity preflight requires an explicit context budget")
        if self.context_window_tokens is not None and self.reasoning_tokenizer_path is None:
            raise ValueError("context budgeting requires reasoning_tokenizer_path")
        if (self.context_window_tokens is not None
                and self.context_safety_tokens + self.minimum_output_tokens >= self.context_window_tokens):
            raise ValueError("context safety and minimum output must fit the context window")
        return self


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_parallel_runs: int = Field(default=6, ge=1, le=256)
    http_retries: int = Field(default=2, ge=0, le=5)
    recovery_successes: int = Field(default=20, ge=1)
    endpoints: tuple[EndpointSettings, ...] = (EndpointSettings(slot=1, max_in_flight=6),)
    # Absent preserves the historical per-run placement and serialized policy.
    role_slots: dict[str, int] | None = Field(default=None, exclude_if=lambda v: v is None)
    # A role may use a deterministic pool of endpoints. Each role within a run
    # remains pinned to one selected endpoint for all of its tool turns.
    role_slot_pools: dict[str, tuple[int, ...]] | None = Field(
        default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def unique(self):
        if not self.endpoints or len({e.slot for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("endpoints must be nonempty with unique slots")
        if self.role_slots is not None:
            if set(self.role_slots) != {"manager", "compliance"}:
                raise ValueError("role_slots must declare exactly manager and compliance")
            if not set(self.role_slots.values()) <= {e.slot for e in self.endpoints}:
                raise ValueError("role_slots must reference declared endpoint slots")
        if self.role_slot_pools is not None:
            if self.role_slots is not None:
                raise ValueError("role_slots and role_slot_pools are mutually exclusive")
            if set(self.role_slot_pools) != {"manager", "compliance"}:
                raise ValueError("role_slot_pools must declare exactly manager and compliance")
            available = {e.slot for e in self.endpoints}
            for role, slots in self.role_slot_pools.items():
                if not slots or len(set(slots)) != len(slots) or not set(slots) <= available:
                    raise ValueError(f"role_slot_pools[{role}] must contain unique declared endpoints")
        group_limits = {}
        for endpoint in self.endpoints:
            if endpoint.quota_group is not None:
                previous = group_limits.setdefault(endpoint.quota_group, endpoint.max_in_flight)
                if previous != endpoint.max_in_flight:
                    raise ValueError("all slots in a quota_group must declare the same max_in_flight")
        return self

    @classmethod
    def load(cls, path):
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class AdaptiveGate:
    """Shared per endpoint, across all runs and both roles. AIMD on throttling."""
    def __init__(self, maximum, recovery=20):
        self.maximum = self.limit = maximum
        self.active = self.good = 0
        self.cooldown = 0.0
        self.recovery = recovery
        self.condition = threading.Condition()

    def acquire(self):
        with self.condition:
            while self.active >= self.limit or time.monotonic() < self.cooldown:
                self.condition.wait(timeout=max(0.05, self.cooldown - time.monotonic())
                                    if time.monotonic() < self.cooldown else None)
            self.active += 1

    def release(self, throttled=False, delay=0.0, success=False):
        with self.condition:
            self.active -= 1
            if throttled:
                self.limit = max(1, self.limit // 2)
                self.good = 0
                self.cooldown = max(self.cooldown, time.monotonic() + delay)
            elif success:
                self.good += 1
                if self.good >= self.recovery:
                    self.limit = min(self.maximum, self.limit + 1)
                    self.good = 0
            self.condition.notify_all()


def retry_delay(headers, attempt):
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    return min(30.0, 2.0 ** (attempt + 1))


class PooledLiveFactory:
    """Pin clients by explicit role slots, or balance whole runs by capacity.

    Keys are held only by adapters. Switching providers during an observation
    is forbidden. Account-level/shared-key quotas must be configured by the
    operator rather than inferred from the number of keys.
    """
    def __init__(self, settings, jobs, directory, *, transport=None, sleeper=time.sleep, capacity_reader=None):
        self.settings = settings
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / f"http_attempts_{uuid4().hex}.jsonl"
        self.lock = threading.Lock()
        self.transport = transport or _urlopen_transport
        self.sleeper = sleeper
        groups = {}
        self.gates = {}
        for endpoint in settings.endpoints:
            group = ("group", endpoint.quota_group) if endpoint.quota_group else ("slot", endpoint.slot)
            if group not in groups:
                groups[group] = AdaptiveGate(endpoint.max_in_flight, settings.recovery_successes)
            self.gates[endpoint.slot] = groups[group]
        self.urls = {}
        self.key_names = {}
        for endpoint in settings.endpoints:
            suffix = "" if endpoint.slot == 1 else f"_{endpoint.slot}"
            url_name, key_name = f"PILOT01_BASE_URL{suffix}", f"PILOT01_API_KEY{suffix}"
            if not os.environ.get(url_name) or not os.environ.get(key_name):
                raise ModelClientError(f"API slot {endpoint.slot} requires {url_name} and {key_name}")
            self.urls[endpoint.slot] = os.environ[url_name]
            self.key_names[endpoint.slot] = key_name
        self.serving_capacities = {}
        for endpoint in settings.endpoints:
            if endpoint.capacity_preflight is not None:
                from .openai_compat import read_sglang_capacity
                from .serving_capacity import validate_sglang_capacity
                info = (capacity_reader or read_sglang_capacity)(self.urls[endpoint.slot])
                self.serving_capacities[str(endpoint.slot)] = validate_sglang_capacity(info, endpoint)
        # Fixed role placement is opt-in; no fallback to the other role's GPU.
        self.role_routing = {}
        jobs = tuple(jobs)
        if settings.role_slots is not None:
            self.role_routing = {job.run_id: dict(settings.role_slots) for job in jobs}
        elif settings.role_slot_pools is not None:
            endpoints = {endpoint.slot: endpoint for endpoint in settings.endpoints}
            assigned = {role: {slot: 0 for slot in slots}
                        for role, slots in settings.role_slot_pools.items()}
            for job in jobs:
                route = {}
                for role in ("manager", "compliance"):
                    slots = settings.role_slot_pools[role]
                    slot = min(slots, key=lambda candidate: (
                        assigned[role][candidate] / endpoints[candidate].max_in_flight,
                        candidate,
                    ))
                    assigned[role][slot] += 1
                    route[role] = slot
                self.role_routing[job.run_id] = route
        # Smooth weighted placement; plan order, no outcome-dependent routing.
        assigned = {e.slot: 0 for e in settings.endpoints}
        self.routing = {}
        for job in (() if self.role_routing else jobs):
            e = min(settings.endpoints, key=lambda e: (assigned[e.slot] / e.max_in_flight, e.slot))
            self.routing[job.run_id] = e.slot
            assigned[e.slot] += 1

    def write_manifest(self, config_path, plan):
        from urllib.parse import urlsplit
        payload = dict(settings=self.settings.model_dump(mode="json"),
            config_sha256=hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
            plan_fingerprint=plan.fingerprint(),
            endpoints={str(slot): dict(host=urlsplit(url).hostname,
                endpoint_sha256=hashlib.sha256(url.encode()).hexdigest())
                for slot, url in self.urls.items()},
            run_slots=self.routing, http_audit=self.log_path.name,
            at=datetime.now(timezone.utc).isoformat())
        if self.role_routing:
            payload["run_role_slots"] = self.role_routing
        if self.serving_capacities:
            payload["serving_capacities"] = self.serving_capacities
        policy = {key: payload[key] for key in ("settings", "config_sha256",
                  "plan_fingerprint", "endpoints", "run_slots")}
        if self.role_routing:
            policy["run_role_slots"] = self.role_routing
        if self.serving_capacities:
            policy["serving_capacities"] = self.serving_capacities
        policy_path = self.directory / "execution_policy.json"
        if policy_path.exists():
            if json.loads(policy_path.read_text(encoding="utf-8")) != policy:
                raise ValueError("execution policy changed; use a new experiment ID/output directory")
        else:
            with policy_path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(policy, indent=2) + "\n")
        path = self.directory / f"execution_{uuid4().hex}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def __call__(self, *, role, job):
        slot = (self.role_routing[job.run_id][role] if self.role_routing
                else self.routing[job.run_id])
        gate = self.gates[slot]

        def send(request, timeout):
            operation = getattr(request, "pilot01_model_call_id", None) or uuid4().hex
            for attempt in range(self.settings.http_retries + 1):
                gate.acquire()
                start = datetime.now(timezone.utc)
                status = None
                failure = None
                delay = 0.0
                retry = False
                try:
                    body = self.transport(request, timeout)
                    status = 200
                    return body
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    delay = retry_delay(exc.headers, attempt)
                    retry = status in (429, 502, 503, 504) and attempt < self.settings.http_retries and delay <= 60
                    if not retry:
                        raise
                    # A retried response is not returned to the model adapter.
                    exc.close()
                except Exception as exc:
                    failure = type(exc).__name__
                    raise
                finally:
                    gate.release(throttled=status in (429, 503),
                                 delay=min(delay, 60), success=status == 200)
                    record = dict(run_id=job.run_id, role=role, slot=slot,
                        operation=operation, model_call_id=getattr(request, "pilot01_model_call_id", None),
                        attempt=attempt, status=status,
                        error_type=failure, retry=retry, retry_delay=delay if retry else None,
                        requested_at=start.isoformat(),
                        responded_at=datetime.now(timezone.utc).isoformat(),
                        concurrency_limit=gate.limit)
                    # Deliberately no headers, response bodies, URLs or exceptions.
                    with self.lock, self.log_path.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(record) + "\n")
                if retry:
                    self.sleeper(delay)
            raise AssertionError("unreachable")

        endpoint = next(e for e in self.settings.endpoints if e.slot == slot)
        return OpenAICompatibleClient(base_url=self.urls[slot],
            api_key_env=self.key_names[slot], transport=send,
            reasoning_stream=endpoint.reasoning_stream,
            reasoning_tokenizer_path=endpoint.reasoning_tokenizer_path,
            tool_transport=endpoint.tool_transport,
            context_window_tokens=endpoint.context_window_tokens,
            context_safety_tokens=endpoint.context_safety_tokens,
            minimum_output_tokens=endpoint.minimum_output_tokens)

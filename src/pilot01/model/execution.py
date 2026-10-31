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
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import ModelClientError
from .openai_compat import OpenAICompatibleClient, _urlopen_transport


class EndpointSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slot: int = Field(ge=1, le=32)
    max_in_flight: int = Field(ge=1, le=128)
    quota_group: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    reasoning_stream: bool = Field(default=False, exclude_if=lambda v: not v)
    reasoning_tokenizer_path: str | None = Field(default=None, exclude_if=lambda v: v is None)


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_parallel_runs: int = Field(default=6, ge=1, le=256)
    http_retries: int = Field(default=2, ge=0, le=5)
    recovery_successes: int = Field(default=20, ge=1)
    endpoints: tuple[EndpointSettings, ...] = (EndpointSettings(slot=1, max_in_flight=6),)
    # Absent preserves the historical per-run placement and serialized policy.
    role_slots: dict[str, int] | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def unique(self):
        if not self.endpoints or len({e.slot for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("endpoints must be nonempty with unique slots")
        if self.role_slots is not None:
            if set(self.role_slots) != {"manager", "compliance"}:
                raise ValueError("role_slots must declare exactly manager and compliance")
            if not set(self.role_slots.values()) <= {e.slot for e in self.endpoints}:
                raise ValueError("role_slots must reference declared endpoint slots")
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
    def __init__(self, settings, jobs, directory, *, transport=None, sleeper=time.sleep):
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
        # Fixed role placement is opt-in; no fallback to the other role's GPU.
        self.role_routing = {}
        jobs = tuple(jobs)
        if settings.role_slots is not None:
            self.role_routing = {job.run_id: dict(settings.role_slots) for job in jobs}
        # Smooth weighted placement; plan order, no outcome-dependent routing.
        assigned = {e.slot: 0 for e in settings.endpoints}
        self.routing = {}
        for job in (() if settings.role_slots is not None else jobs):
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
        if self.settings.role_slots is not None:
            payload["run_role_slots"] = self.role_routing
        policy = {key: payload[key] for key in ("settings", "config_sha256",
                  "plan_fingerprint", "endpoints", "run_slots")}
        if self.settings.role_slots is not None:
            policy["run_role_slots"] = self.role_routing
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
        slot = (self.role_routing[job.run_id][role] if self.settings.role_slots is not None
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
            reasoning_tokenizer_path=endpoint.reasoning_tokenizer_path)

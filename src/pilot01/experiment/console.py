"""Loopback-only experiment console. Opening the page never sends a model request."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import yaml

from ..model.execution import ExecutionSettings, PooledLiveFactory
from ..model.client import ModelClientError
from ..runtime import ExperimentProfile
from .artifacts import RunArtifact
from .batch import ExperimentRunner
from .cases import CaseRegistry
from .development.neutral_materials import verify_ready_registry
from .layout import ExperimentPaths
from .runspec import RunPlan, build_run_plan, write_run_plan_csv
from .scoring_v2 import write_report
from .records import Records, MAX_UPLOAD, MAX_ZIP_UPLOAD
from .design import ExperimentDesign, COHORT_NAMES
from .gpu_monitor import GpuMonitor

ROOT = Path(__file__).resolve().parents[3]

LOCAL_GPU_ENDPOINTS = (
    dict(gpu_index=0, slot=1, base_url="http://127.0.0.1:30000/v1", port=30000),
    dict(gpu_index=1, slot=2, base_url="http://127.0.0.1:30001/v1", port=30001),
    dict(gpu_index=2, slot=3, base_url="http://127.0.0.1:30002/v1", port=30002),
)
LOCAL_GPU_MAX_IN_FLIGHT = 4
LOCAL_REASONING_TOKENIZER = "/home/evan/ai/models/Qwen3.8-27B/tokenizer.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def read_lines(path):
    """Only complete LF-delimited records; tolerate a writer's unfinished tail."""
    try:
        with Path(path).open("rb") as f:
            return [json.loads(line) for line in f if line.endswith(b"\n") and line.strip()]
    except FileNotFoundError:
        return []


def api_values(path):
    """Private file reader: never return this mapping over HTTP."""
    values = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError("本地API配置格式错误，请使用 NAME=value")
            name, value = line.split("=", 1)
            if not re.fullmatch(r"PILOT01_(?:MODEL_ID|(?:BASE_URL|API_KEY)(?:_(?:[2-9]|[12][0-9]|3[0-2]))?)", name):
                raise ValueError("本地API配置存在不支持的字段")
            value = value.strip().strip('\"').strip("'")
            values[name] = value
    return values


def valid_api_url(url):
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not any(c.isspace() or c in "\"'" for c in url)
                and not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment and not parsed.path.rstrip("/").endswith("/chat/completions"))
    except ValueError:
        return False


def local_credentials(path):
    """Return public endpoint settings and a key fingerprint, never key text."""
    values = api_values(path)
    status = []
    for slot in (1, 2):
        suffix = "" if slot == 1 else f"_{slot}"
        key_name, url_name = f"PILOT01_API_KEY{suffix}", f"PILOT01_BASE_URL{suffix}"
        key, url = values.get(key_name, ""), values.get(url_name, "")
        parsed = urlsplit(url)
        valid_url = valid_api_url(url)
        valid_key = bool(key) and not any(p in key.lower() for p in ("replace-with", "填", "your-key"))
        for name in (key_name, url_name):
            if values.get(name):
                os.environ[name] = values[name]
            else:
                os.environ.pop(name, None)
        # Invalid legacy URLs are not returned: they may contain credentials.
        status.append(dict(slot=slot, ready=bool(valid_url and valid_key), host=parsed.hostname or "未配置",
                           base_url=url if valid_url else "", key_present=valid_key,
                           key_id=hashlib.sha256(key.encode()).hexdigest()[:10] if valid_key else None))
    return status


def local_gpu_credentials():
    """Install the three loopback-only SGLang endpoints without exposing secrets."""
    status = []
    for endpoint in LOCAL_GPU_ENDPOINTS:
        slot = endpoint["slot"]
        suffix = "" if slot == 1 else f"_{slot}"
        key_name, url_name = f"PILOT01_API_KEY{suffix}", f"PILOT01_BASE_URL{suffix}"
        # The local SGLang services accept an arbitrary bearer value. It is kept
        # process-local and is never returned to the browser or written to a run.
        os.environ[url_name] = endpoint["base_url"]
        os.environ[key_name] = "local-loopback"
        status.append(dict(**endpoint, ready=True, host="127.0.0.1",
                           key_present=True, key_id="local-only",
                           max_in_flight=LOCAL_GPU_MAX_IN_FLIGHT))
    return status


class ObservedClient:
    """Add console activity telemetry without changing the provider contract."""

    def __init__(self, inner, activity, role, job):
        self.inner = inner
        self.activity = activity
        self.role = role
        self.job = job

    @property
    def tool_transport(self):
        # Native-v2 validates this capability before the first model request.
        # The observer must remain transparent to that protocol negotiation.
        return getattr(self.inner, "tool_transport", "legacy-text")

    def prepare_request(self, request):
        prepare = getattr(self.inner, "prepare_request", None)
        return request if prepare is None else prepare(request)

    def estimate_request_tokens(self, request):
        estimate = getattr(self.inner, "estimate_request_tokens", None)
        if estimate is None:
            raise ModelClientError("observed model client has no local token estimator")
        return estimate(request)

    def generate(self, request):
        self.activity(self.job, self.role, "requesting")
        try:
            return self.inner.generate(request)
        finally:
            self.activity(self.job, self.role, "returned")


class Console:
    def __init__(self, root=ROOT, *, registry=None, profile=None, case_ids=None,
                 factory_builder=None, credential_loader=None, gpu_monitor=None,
                 execution_backend=None):
        self.root = Path(root)
        self.output = self.root / "outputs/experiment_console"
        self.output.mkdir(parents=True, exist_ok=True)
        self.execution_backend = execution_backend or os.environ.get(
            "PILOT01_EXECUTION_BACKEND", "local-gpu" if credential_loader is None else "api")
        if self.execution_backend not in ("local-gpu", "api"):
            raise ValueError("PILOT01_EXECUTION_BACKEND must be local-gpu or api")
        self.registry = registry or verify_ready_registry(self.root / "data/pilot01/clean80_v1/case_registry_v2.json")
        default_profile = ("experiment_native_tools_closed_v4.yaml" if self.execution_backend == "local-gpu"
                           else "experiment_v3.yaml")
        self.profile = profile or ExperimentProfile(self.root / "config" / default_profile)
        self.loaded_fingerprint = self.profile.fingerprint()
        cohorts = read_json(self.root / "data/pilot01/clean80_v1/researcher_cohorts.json", [])
        if case_ids is None:
            case_ids = self.registry.case_ids
        self.case_ids = tuple(case_ids)
        self.design = ExperimentDesign(self.registry, self.case_ids, cohorts)
        self.factorial = self.profile.conditions.conditions_version == "3"
        self.draft_selection = None
        self.factory_builder = factory_builder or PooledLiveFactory
        self.credential_loader = credential_loader or (
            local_gpu_credentials if self.execution_backend == "local-gpu"
            else lambda: local_credentials(self.root / "config/api.local.env"))
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.thread = None
        self.stop_event = threading.Event()
        self.pool = None
        self.session = None
        self.draft = None
        self.plan = None
        self.activity = {}
        self.cache = {}
        self.http_counters = {}
        self.state_versions = OrderedDict()
        self.state_epoch = uuid4().hex
        self.state_serial = 0
        self.configured_slots = self.credential_loader()
        self.gpu_monitor = gpu_monitor or GpuMonitor()
        self.redaction_keys = {v for k, v in os.environ.items() if k.startswith("PILOT01_API_KEY") and v}
        self.records = Records(self.output, self.registry, redact=self.redact,
            active_experiment=lambda: self.session["experiment_id"] if self.session and self.busy() else None)
        self._recover_latest()

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def gpu_status(self, force=False):
        """Read-only hardware status; collecting it never starts a model request."""
        return self.gpu_monitor.collect(force=force)

    def _recover_latest(self):
        files = list(self.output.glob("manifests/*/console_session.json"))
        if files:
            path = max(files, key=lambda p: p.stat().st_mtime)
            session = read_json(path)
            if session:
                self.session = session
                self.plan = RunPlan.load_json(path.parent / "run_plan.json")
                self.session.setdefault("conditions", list(self.plan.condition_ids))
                self.session.setdefault("repeat", self.plan.repeat_count)
                if session["status"] in ("running", "stopping"):
                    self.session["status"] = "interrupted"
                    self.session["message"] = "上次操作台已退出；已有结果保留，可继续未完成项。"
                for event in read_lines(path.parent / "console_activity.jsonl"):
                    if event.get("run_id"):
                        self.activity.setdefault(event["run_id"], {}).update(event)

    def _paths(self, experiment_id=None):
        return ExperimentPaths(root=self.output, experiment_id=experiment_id or self.session["experiment_id"])

    def _save(self):
        write_json(self._paths().manifest_dir / "console_session.json", self.session)

    def config(self):
        with self.lock:
            if not self.busy():
                self.configured_slots = self.credential_loader()
            model = self.profile.models.for_role("manager")
            model_parameters = model.model_dump(mode="json")
            return dict(apis=self.configured_slots, execution_backend=self.execution_backend,
                        dataset="全部80份 · 自定义因子实验" if self.factorial else "D条件开发预试",
                        contracts=len(self.case_ids), model=model.model_id,
                        model_parameters=model_parameters,
                        runtime_parameters=self.profile.runtime.model_dump(mode="json"),
                        tool_limit=self.profile.runtime.max_tool_requests_per_node,
                        output_limit=model.max_output_tokens,
                        default_count=64,
                        default_concurrency=4 if self.execution_backend == "local-gpu" else 16,
                        max_concurrency=12 if self.execution_backend == "local-gpu" else 128,
                        gpu_max_in_flight=LOCAL_GPU_MAX_IN_FLIGHT,
                        role_gpu_defaults=dict(manager=[0], compliance=[1]),
                        server_parameters=dict(power_limit_w=200, context_length=49152,
                            precision="BF16", speculative="NEXTN/MTP", max_running_requests=4,
                            reasoning_parser="qwen3", tool_call_parser="qwen3_coder",
                            context_safety_tokens=1024, minimum_output_tokens=2048),
                        factorial=self.factorial, profile_version=self.profile.version,
                        cohorts=[dict(id=key, label=label, contracts=len(self.design.cases(key)),
                                      memos=sum(len(self.registry.get(cid).error_conditions) for cid in self.design.cases(key)))
                                 for key, label in COHORT_NAMES.items()])

    def estimate(self, data):
        return self.design.estimate(data)

    def save_apis(self, data):
        """Atomically replace user-entered endpoints; a blank key keeps its value."""
        with self.lock:
            if self.execution_backend == "local-gpu":
                raise ValueError("本地GPU端点由服务器启动配置固定，不能在网页中改写")
            if self.busy():
                raise ValueError("批次正在运行，结束后才能修改API配置")
            if set(data) != {"apis"} or not isinstance(data["apis"], list) or len(data["apis"]) != 2:
                raise ValueError("请提交API 1和API 2的完整配置")
            current = api_values(self.root / "config/api.local.env")
            updated = dict(current)
            seen = set()
            for item in data["apis"]:
                if not isinstance(item, dict) or set(item) != {"slot", "base_url", "api_key"}:
                    raise ValueError("API配置字段不正确，模型由实验配置固定")
                slot, url, key = item["slot"], item["base_url"], item["api_key"]
                if type(slot) is not int or slot not in (1, 2) or slot in seen:
                    raise ValueError("API编号必须为1和2，且不能重复")
                seen.add(slot)
                if not isinstance(url, str) or not isinstance(key, str):
                    raise ValueError("地址和Key必须为文本")
                url, key = url.strip().rstrip("/"), key.strip()
                if len(url) > 2048 or len(key) > 8192 or any(c.isspace() or c in "\"'" for c in key):
                    raise ValueError("Key不能含空白或引号，地址和Key不能过长")
                suffix = "" if slot == 1 else "_2"
                key_name, url_name = f"PILOT01_API_KEY{suffix}", f"PILOT01_BASE_URL{suffix}"
                key = key or current.get(key_name, "")
                if (url or key) and not valid_api_url(url):
                    raise ValueError(f"API {slot} 地址须为http(s)接口根地址，不能含账号密码、查询参数或/chat/completions")
                if url and not key:
                    raise ValueError(f"API {slot} 尚未填写Key")
                if key and any(p in key.lower() for p in ("replace-with", "填", "your-key")):
                    raise ValueError(f"API {slot} 需要有效Key，不能使用占位符")
                updated[url_name], updated[key_name] = url, key
            path = self.root / "config/api.local.env"
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            try:
                with temp.open("x", encoding="utf-8", newline="\n") as handle:
                    temp.chmod(0o600)
                    handle.write("# Managed by the experiment console; never publish keys.\n")
                    handle.writelines(f"{name}={value}\n" for name, value in sorted(updated.items()))
                temp.replace(path)
                path.chmod(0o600)
            finally:
                temp.unlink(missing_ok=True)
            self.redaction_keys.update(v for k, v in {**current, **updated}.items() if k.startswith("PILOT01_API_KEY") and v)
            self.redaction_keys.update(v for k, v in current.items() if k.startswith("PILOT01_API_KEY") and v)
            self.configured_slots = self.credential_loader()
            return dict(apis=self.configured_slots, message="API配置已保存；新批次使用新配置，Key留空保留原值")

    def api_snapshot(self, slots):
        return [{k: s.get(k) for k in ("slot", "gpu_index", "host", "base_url", "key_id", "max_in_flight")}
                for s in sorted(self.configured_slots, key=lambda s: s["slot"]) if s["slot"] in slots]

    def materials(self, cohort="all80"):
        return dict(materials=self.design.catalog(cohort))

    def material(self, case_id):
        return self.design.preview(case_id)

    def prepare(self, data):
        with self.lock:
            if self.profile.fingerprint() != self.loaded_fingerprint:
                raise ValueError("源码或配置已更新，请重启操作台后再生成清单")
            if self.busy():
                raise ValueError("当前批次仍在运行，请等它结束或停止派发后再创建新批次")
            selection = self.estimate(data) if self.factorial else None
            conditions = tuple(selection["conditions"]) if selection else tuple(data.get("conditions", ("D0", "D1", "D2")))
            if not conditions or not set(conditions) <= set(self.profile.conditions.main_conditions) or len(set(conditions)) != len(conditions):
                raise ValueError("没有有效的实验组合；A0V1不能运行，请调整条件")
            if selection is not None and not selection["valid"]:
                raise ValueError("所选范围没有可用memo，请调整E条件或合同范围")
            repeat = data.get("repeat", 1)
            if type(repeat) is not int or not 1 <= repeat <= 10:
                raise ValueError("重复次数必须为1到10的整数")
            experiment_id = ("console-factorial-" if self.factorial else "console-preflight-") + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
            plan = build_run_plan(experiment_id=experiment_id, registry=self.registry,
                conditions=self.profile.conditions, models=self.profile.models, repeat_count=repeat,
                seed=20261018, case_ids=selection["case_ids"] if selection else self.case_ids, condition_ids=conditions,
                runtime_config=self.profile.runtime, profile_fingerprint=self.loaded_fingerprint,
                error_conditions=selection["e"] if selection else None)
            self.draft = plan
            self.draft_selection = selection
            return dict(draft_id=experiment_id, total=len(plan.jobs), repeat=repeat,
                        selection=selection,
                        jobs=[self.job_row(i, j) for i, j in enumerate(plan.jobs, 1)])

    @staticmethod
    def job_row(i, job):
        verification_level = "V1" if job.verification_required else "V0"
        return dict(index=i, run_id=job.run_id, case_id=job.case_id,
                    a="A1" if job.source_access else "A0", v="V2" if job.recovery_required else verification_level,
                    d=job.condition_id[-2:] if re.search(r"D[012]$", job.condition_id) else None,
                    condition=job.condition_id, memo=job.error_condition.value, repeat=job.repeat_index + 1)

    def start(self, data, *, initiated_by=None):
        with self.lock:
            if initiated_by is not None and not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", initiated_by):
                raise ValueError("登录用户信息不合法")
            if self.busy():
                raise ValueError("已有一个批次正在运行")
            if not self.draft or data.get("draft_id") != self.draft.experiment_id:
                raise ValueError("清单已过期，请重新生成运行清单")
            if self.session and self.session.get("experiment_id") == self.draft.experiment_id:
                raise ValueError("这份清单已经启动过，不能用“开始”覆盖原尝试；请继续未完成项或新建一轮")
            selected = data.get("run_ids", [])
            if not isinstance(selected, list) or not selected or not all(isinstance(r, str) for r in selected):
                raise ValueError("请至少选择一个run")
            if len(set(selected)) != len(selected) or not set(selected) <= set(self.draft.run_ids):
                raise ValueError("选择的run不属于当前清单")
            self.configured_slots = self.credential_loader()
            ready = {s["slot"] for s in self.configured_slots if s["ready"]}
            concurrency = data.get("concurrency", 4 if self.execution_backend == "local-gpu" else 64)
            maximum = 12 if self.execution_backend == "local-gpu" else 128
            if type(concurrency) is not int or not 1 <= concurrency <= maximum:
                raise ValueError(f"并发数必须为1到{maximum}的整数")
            role_gpus = None
            if self.execution_backend == "local-gpu":
                role_gpus = {}
                gpu_to_slot = {s["gpu_index"]: s["slot"] for s in self.configured_slots if s["ready"]}
                for role in ("manager", "compliance"):
                    gpus = data.get(f"{role}_gpus", [])
                    if (not isinstance(gpus, list) or not gpus
                            or any(type(gpu) is not int for gpu in gpus)
                            or len(set(gpus)) != len(gpus) or not set(gpus) <= set(gpu_to_slot)):
                        raise ValueError(f"{role}必须至少选择一个已就绪的GPU，且不能重复")
                    role_gpus[role] = list(gpus)
                role_slot_pools = {role: tuple(gpu_to_slot[gpu] for gpu in gpus)
                                   for role, gpus in role_gpus.items()}
                slots = sorted(set(role_slot_pools["manager"]) | set(role_slot_pools["compliance"]))
                tokenizer = os.environ.get("PILOT01_REASONING_TOKENIZER_PATH", LOCAL_REASONING_TOKENIZER)
                settings = ExecutionSettings(max_parallel_runs=concurrency, http_retries=0,
                    recovery_successes=20, endpoints=[dict(slot=slot,
                        max_in_flight=LOCAL_GPU_MAX_IN_FLIGHT, reasoning_stream=True,
                        reasoning_tokenizer_path=tokenizer, tool_transport="native",
                        context_window_tokens=49152, context_safety_tokens=1024,
                        capacity_preflight="sglang-v1",
                        minimum_output_tokens=2048) for slot in slots],
                    role_slot_pools=role_slot_pools)
            else:
                slots = data.get("slots", [])
                if (not slots or any(type(s) is not int for s in slots)
                        or len(set(slots)) != len(slots) or not set(slots) <= ready):
                    raise ValueError("所选API尚未配置完整，请填写config/api.local.env或只选已就绪的API")
                # Legacy API mode preserves the historical shared-account
                # quota and two-attempt transient HTTP recovery behavior.
                settings = ExecutionSettings(max_parallel_runs=concurrency, http_retries=2,
                    recovery_successes=20, endpoints=[dict(slot=s, max_in_flight=concurrency,
                        quota_group="shared-account") for s in slots])
            api_snapshot = self.api_snapshot(slots)
            plan = self.draft
            paths = self._paths(plan.experiment_id).ensure()
            plan.check(self.profile.conditions, self.registry)
            if plan.profile_fingerprint != self.profile.fingerprint() or plan.profile_fingerprint != self.loaded_fingerprint:
                raise ValueError("程序版本已变化，请重新生成清单")
            plan.write_json(paths.run_plan_json)
            write_run_plan_csv(plan, paths.run_plan_csv)
            config_path = paths.manifest_dir / "console_execution.yaml"
            config_path.write_text(yaml.safe_dump(settings.model_dump(mode="json")), encoding="utf-8")
            selected_set = set(selected)
            selected_jobs = [job for job in plan.jobs if job.run_id in selected_set]
            pool = self.factory_builder(settings, selected_jobs, paths.manifest_dir)
            pool.write_manifest(config_path, plan)
            selected = [job.run_id for job in selected_jobs]
            write_json(paths.manifest_dir / "launch_selection.json", dict(
                purpose="User-configured A/V/E/D factorial experiment" if self.factorial else "D-only development preflight",
                factor_selection=self.draft_selection,
                full_plan_count=len(plan.jobs), selected_count=len(selected), run_ids=selected,
                api_configuration=api_snapshot,
                execution_backend=self.execution_backend,
                role_gpus=role_gpus,
                model_parameters=self.profile.models.for_role("manager").model_dump(mode="json"),
                runtime_parameters=self.profile.runtime.model_dump(mode="json"),
                initiated_by=initiated_by,
                identity_source="gateway_header" if initiated_by else "not_recorded",
                selection="manual selection from fixed-seed full plan; may be an incomplete treatment grid",
                started_at=now(), settings=settings.model_dump(mode="json")))
            self.plan, self.pool = plan, pool
            self.activity, self.cache = {}, {}
            self.session = dict(experiment_id=plan.experiment_id, status="running", run_ids=selected,
                created_at=now(), finished_at=None, selected_count=len(selected), full_plan_count=len(plan.jobs),
                concurrency=concurrency, slots=slots, api_configuration=api_snapshot, error=None, output_path=str(paths.root),
                execution_backend=self.execution_backend, manager_gpus=(role_gpus or {}).get("manager"),
                compliance_gpus=(role_gpus or {}).get("compliance"),
                conditions=list(plan.condition_ids), repeat=plan.repeat_count,
                factor_selection=self.draft_selection,
                initiated_by=initiated_by,
                identity_source="gateway_header" if initiated_by else "not_recorded",
                message="参数已冻结；未选中的计划项不计为运行失败，任意选取不保证完整配对。")
            self._launch(resume=False)
            return dict(experiment_id=plan.experiment_id, selected=len(selected), concurrency=concurrency,
                        manager_gpus=(role_gpus or {}).get("manager"),
                        compliance_gpus=(role_gpus or {}).get("compliance"))

    def _launch(self, resume):
        self.stop_event.clear()
        self.session["status"] = "running"
        self.session["finished_at"] = None
        self.session["error"] = None
        self._save()
        self.thread = threading.Thread(target=self._execute, args=(resume,), daemon=True,
                                       name="experiment-console-batch")
        self.thread.start()

    def _activity(self, job, role, phase):
        with self.lock:
            previous = self.activity.get(job.run_id, {})
            timestamp = now()
            previous_role = previous.get("role")
            role_changed = bool(previous_role and previous_role != role)
            handoff_at = previous.get("handoff_at")
            if previous_role == "manager" and role == "compliance":
                handoff_at = timestamp
            record = dict(run_id=job.run_id, role=role, phase=phase,
                          first_started_at=previous.get("first_started_at", timestamp),
                          role_started_at=(timestamp if role_changed
                                           else previous.get("role_started_at", timestamp)),
                          previous_role=(previous_role if role_changed
                                         else previous.get("previous_role")),
                          handoff_at=handoff_at, updated_at=timestamp)
            self.activity[job.run_id] = record
            with (self._paths().manifest_dir / "console_activity.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def _execute(self, resume):
        outer = self
        pool = self.pool
        def factory(*, role, job):
            return ObservedClient(pool(role=role, job=job), outer._activity, role, job)
        paths = self._paths()
        try:
            runner = ExperimentRunner(plan=self.plan, registry=self.registry,
                conditions=self.profile.conditions, workflow=self.profile.workflow,
                models=self.profile.models, client_factory=factory, paths=paths, profile=self.profile)
            batch = runner.run(max_workers=self.session["concurrency"], resume=resume,
                selected_run_ids=self.session["run_ids"], stop_event=self.stop_event)
            batch.write_json(paths.batch_index_json)
            # Keep the FULL plan denominator in the research report; the console
            # shows a separate selected-run progress denominator.
            write_report(self.plan, self.registry, paths.raw_dir, paths.processed_dir)
            with self.lock:
                self.session["status"] = "stopped" if self.stop_event.is_set() and self._pending_count() else "finished"
        except Exception as exc:
            with self.lock:
                self.session["status"] = "error"
                self.session["error"] = self.redact(str(exc))
        finally:
            with self.lock:
                self.session["finished_at"] = now()
                self._save()

    def redact(self, text):
        for value in self.redaction_keys:
            text = text.replace(value, "[REDACTED]")
        for name, value in os.environ.items():
            if name.startswith("PILOT01_API_KEY") and value:
                text = text.replace(value, "[REDACTED]")
        return text

    def stop(self):
        with self.lock:
            if not self.busy():
                raise ValueError("当前没有运行中的批次")
            self.stop_event.set()
            self.session["status"] = "stopping"
            self._save()
            return dict(message="已停止派发新run；已开始的run会继续结束并保留结果。")

    def _pending_count(self):
        if not self.session:
            return 0
        return sum(not (self._paths().run_dir(r) / "run.json").exists()
                   for r in self.session["run_ids"])

    def resume(self):
        with self.lock:
            if self.busy() or not self.session or self.session["status"] not in ("stopped", "interrupted", "error"):
                raise ValueError("当前没有可继续的中断批次")
            if not self._pending_count():
                raise ValueError("本轮所有run均已结束（包括已记录的失败），请新建一轮")
            if self.plan.profile_fingerprint != self.profile.fingerprint() or self.plan.profile_fingerprint != self.loaded_fingerprint:
                raise ValueError("代码或配置版本已改变，不能把新版本混入旧批次，请创建新清单")
            self.configured_slots = self.credential_loader()
            ready = {s["slot"] for s in self.configured_slots if s["ready"]}
            if not set(self.session["slots"]) <= ready:
                raise ValueError("原批次所需的API未配置完整")
            if self.session.get("api_configuration") is not None and self.session["api_configuration"] != self.api_snapshot(self.session["slots"]):
                raise ValueError("API地址或Key已改变，不能混入原批次；请恢复原配置或新建一轮")
            paths = self._paths()
            config_path = paths.manifest_dir / "console_execution.yaml"
            settings = ExecutionSettings.load(config_path)
            selected = set(self.session["run_ids"])
            jobs = [job for job in self.plan.jobs if job.run_id in selected]
            self.pool = self.factory_builder(settings, jobs, paths.manifest_dir)
            self.pool.write_manifest(config_path, self.plan)
            self._launch(resume=True)
            return dict(message="继续未完成项；已完成及已记录失败的run不会重跑。")

    def cached_file(self, path, lines=False):
        path = Path(path)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return [] if lines else None
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        cached = self.cache.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
        value = read_lines(path) if lines else read_json(path)
        self.cache[str(path)] = (key, value)
        return value

    def _http_totals(self, directory):
        totals = Counter()
        for path in directory.glob("http_attempts_*.jsonl"):
            stat = path.stat()
            previous = self.http_counters.get(str(path))
            identity = (stat.st_ino, stat.st_ctime_ns)
            # Appends can change ctime on Linux; inode + size/mtime distinguish
            # truncation/replacement without resetting on an ordinary append.
            if not previous or previous[0][0] != identity[0] or stat.st_size < previous[1] or (stat.st_size == previous[1] and stat.st_mtime_ns != previous[2]):
                offset, counts = 0, Counter()
            else:
                offset, counts = previous[1], previous[3].copy()
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    if line.strip():
                        a = json.loads(line)
                        counts.update(http_attempts=1, http_retries=a.get("attempt", 0) > 0,
                                      rate_limits=a.get("status") == 429, errors_5xx=(a.get("status") or 0) >= 500)
                    offset = stream.tell()
            self.http_counters[str(path)] = (identity, offset, stat.st_mtime_ns, counts)
            totals.update(counts)
        return {key: totals[key] for key in ("http_attempts", "http_retries", "rate_limits", "errors_5xx")}

    def state(self, since=None, *, compact=False):
        with self.lock:
            if not self.session:
                return dict(session=None, rows=[], counters={}, api_limits=[], delta=False, version=None)
            paths = self._paths()
            indexed = {r["run_id"]: r for r in self.records.indexed_rows("local:" + self.session["experiment_id"])}
            rows = []
            for i, job in enumerate(self.plan.jobs, 1):
                if job.run_id not in indexed:
                    continue
                row = self.job_row(i, job)
                summary = indexed[job.run_id]
                activity = self.activity.get(job.run_id, {})
                status = summary["status"]
                if status in ("waiting", "running", "interrupted") and activity:
                    status = "running" if self.busy() else "interrupted"
                start = activity.get("first_started_at") or summary.get("first_started_at")
                end = summary.get("last_responded_at") if status in ("completed", "failed") else None
                elapsed = 0
                if start:
                    end = end or (now() if self.busy() else self.session.get("finished_at") or activity.get("updated_at") or start)
                    elapsed = max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
                role_route = self.pool.role_routing.get(job.run_id, {}) if self.pool and getattr(self.pool, "role_routing", None) else {}
                shared_slot = self.pool.routing.get(job.run_id) if self.pool and getattr(self.pool, "routing", None) else None
                row.update(status=status, node=activity.get("role"), phase=activity.get("phase"),
                    activity_updated_at=activity.get("updated_at"),
                    role_started_at=activity.get("role_started_at"),
                    previous_node=activity.get("previous_role"),
                    handoff_at=activity.get("handoff_at"), elapsed=elapsed,
                    calls=summary.get("calls", 0), tools=summary.get("tools", 0), tokens=summary.get("tokens") or 0,
                    missing_usage=summary.get("missing_usage", 0), action=summary.get("action"),
                    manager_action=summary.get("manager_action"),
                    error=summary.get("error") or "", slot=shared_slot,
                    manager_slot=role_route.get("manager"), compliance_slot=role_route.get("compliance"))
                for role in ("manager", "compliance"):
                    row[role + "_validation_pass"] = summary.get(role + "_validation_pass")
                    row[role + "_audit_failures"] = summary.get(role + "_audit_failures", [])
                rows.append(row)
            counts = Counter(r["status"] for r in rows)
            api_limits = []
            if self.pool:
                seen = set()
                for slot, gate in self.pool.gates.items():
                    if id(gate) not in seen:
                        seen.add(id(gate))
                        api_limits.append(dict(slots=[s for s, g in self.pool.gates.items() if g is gate],
                            active=gate.active, limit=gate.limit, maximum=gate.maximum))
            session = dict(self.session)
            if compact:
                session.pop("run_ids", None)
            pending = counts["waiting"] + counts["interrupted"] + counts["running"]
            if not self.busy() and not pending and session["status"] in ("stopped", "interrupted"):
                session["status"] = "finished"
            profile_unchanged = self.plan.profile_fingerprint == self.loaded_fingerprint
            snapshot = {r["run_id"]: r for r in rows}
            latest = next(reversed(self.state_versions.values()), None) if self.state_versions else None
            if latest != snapshot:
                self.state_serial += 1
                self.state_versions[f"{self.state_epoch}:{self.state_serial}"] = snapshot
                if len(self.state_versions) > 8:
                    self.state_versions.popitem(last=False)
            version = next(reversed(self.state_versions))
            previous = self.state_versions.get(since)
            delta = previous is not None
            sent_rows = [r for rid, r in snapshot.items() if previous.get(rid) != r] if delta else rows
            return dict(session=session, rows=sent_rows, counters=dict(counts), delta=delta, version=version,
                removed=[rid for rid in previous if rid not in snapshot] if delta else [],
                can_resume=not self.busy() and pending > 0 and profile_unchanged
                           and session["status"] in ("stopped", "interrupted", "error"),
                resume_blocked_reason=("实现版本已更新；保留本轮记录并新建一轮"
                    if pending > 0 and not profile_unchanged else None),
                api_limits=api_limits, known_tokens=sum(r["tokens"] for r in rows),
                **self._http_totals(paths.manifest_dir), busy=self.busy())

    def detail(self, run_id):
        with self.lock:
            if not self.session or run_id not in self.session["run_ids"]:
                raise ValueError("该run不在当前批次中")
            directory = self._paths().run_dir(run_id)
            result = dict(run_id=run_id, artifact=read_json(directory / "run.json"),
                calls=read_lines(directory / "model_calls.jsonl"),
                tools=read_lines(directory / "tool_calls.jsonl"),
                verification=read_lines(directory / "verification.jsonl"), directory=str(directory))
            # Redact known secrets even if an unexpected historical error echoed one.
            return json.loads(self.redact(json.dumps(result, ensure_ascii=False)))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def viewer(self):
            user = self.headers.get("X-Pilot01-User")
            return user if user and re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", user) else None

        def send(self, code, body, kind="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_archive(self, ident):
            with app.records.export_zip(ident) as archive:
                size = archive.seek(0, 2)
                archive.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", ident)
                self.send_header("Content-Disposition", f'attachment; filename="experiment-records-{name}.zip"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                shutil.copyfileobj(archive, self.wfile, length=1024 * 1024)

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, dict(error="仅允许本机访问"))
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/":
                    html = Path(__file__).with_name("console.html").read_text(encoding="utf-8")
                    return self.send(200, html.replace("__CSRF_TOKEN__", app.token).encode(), "text/html; charset=utf-8")
                if parsed.path in ("/pixel-office.css", "/pixel-office.js",
                                   "/assets/pixel-office-room.png", "/assets/pixel-office-room-v2.png"):
                    if parsed.path == "/pixel-office.css":
                        path, kind = Path(__file__).with_name("pixel-office.css"), "text/css; charset=utf-8"
                    elif parsed.path == "/pixel-office.js":
                        path, kind = Path(__file__).with_name("pixel-office.js"), "application/javascript; charset=utf-8"
                    else:
                        filename = ("pixel-office-room-v2.png" if parsed.path.endswith("-v2.png")
                                    else "pixel-office-room.png")
                        path = Path(__file__).with_name("assets") / filename
                        kind = "image/png"
                    return self.send(200, path.read_bytes(), kind)
                if parsed.path in ("/office", "/records", "/records.js", "/materials", "/materials.js"):
                    filename = parsed.path.lstrip("/")
                    if not filename.endswith(".js"):
                        filename += ".html"
                    body = Path(__file__).with_name(filename).read_text(encoding="utf-8")
                    return self.send(200, body.replace("__CSRF_TOKEN__", app.token).encode(),
                                     "text/html; charset=utf-8" if filename.endswith(".html") else "application/javascript; charset=utf-8")
                if parsed.path.startswith("/api/records"):
                    args = parse_qs(parsed.query)
                    ident = args.get("id", [""])[0]
                    if parsed.path == "/api/records":
                        return self.send(200, dict(batches=app.records.list()))
                    if parsed.path == "/api/records/batch":
                        return self.send(200, app.records.batch(ident,
                            offset=int(args.get("offset", ["0"])[0]), limit=int(args.get("limit", ["100"])[0]),
                            status=args.get("status", [""])[0], q=args.get("q", [""])[0], condition=args.get("condition", [""])[0]))
                    if parsed.path == "/api/records/run":
                        return self.send(200, app.records.research.section(ident, args.get("run_id", [""])[0],
                            args.get("section", ["overview"])[0], offset=int(args.get("offset", ["0"])[0]),
                            limit=int(args.get("limit", ["100"])[0]), call_id=args.get("call_id", [None])[0]))
                    if parsed.path == "/api/records/export":
                        if args.get("format", ["zip"])[0] == "json":
                            return self.send(200, app.records.export(ident))
                        return self.send_archive(ident)
                if parsed.path == "/api/health":
                    return self.send(200, dict(app="pilot01-console", ready=True))
                if parsed.path == "/api/gpu-status":
                    args = parse_qs(parsed.query)
                    payload = app.gpu_status(force=args.get("force") == ["1"])
                    return self.send(200 if payload.get("ok") else 503, payload)
                if parsed.path == "/api/config":
                    return self.send(200, dict(app.config(), viewer_user=self.viewer()))
                if parsed.path == "/api/materials":
                    return self.send(200, app.materials(parse_qs(parsed.query).get("cohort", ["all80"])[0]))
                if parsed.path == "/api/materials/case":
                    return self.send(200, app.material(parse_qs(parsed.query).get("case_id", [""])[0]))
                if parsed.path == "/api/state":
                    return self.send(200, app.state(parse_qs(parsed.query).get("since", [None])[0], compact=True))
                if parsed.path == "/api/detail":
                    args = parse_qs(parsed.query)
                    rid = args.get("run_id", [""])[0]
                    with app.lock:
                        if not app.session or rid not in app.session["run_ids"]:
                            raise ValueError("该run不在当前批次中")
                        ident = "local:" + app.session["experiment_id"]
                    return self.send(200, app.records.research.section(ident, rid,
                        args.get("section", ["overview"])[0], offset=int(args.get("offset", ["0"])[0]),
                        limit=int(args.get("limit", ["100"])[0]), call_id=args.get("call_id", [None])[0]))
                if parsed.path == "/api/export.csv":
                    rows = app.state()["rows"]
                    out = io.StringIO(newline="")
                    columns = ["run_id", "case_id", "condition", "a", "v", "d", "memo", "repeat", "status", "node", "elapsed", "calls", "tools", "tokens", "action", "error",
                               "manager_validation_pass", "manager_audit_failures", "compliance_validation_pass", "compliance_audit_failures"]
                    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({k: ("'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v) for k, v in row.items()})
                    return self.send(200, ("\ufeff" + out.getvalue()).encode(), "text/csv; charset=utf-8")
                return self.send(404, dict(error="未找到"))
            except (ValueError, OSError, ModelClientError) as exc:
                self.send(400, dict(error=app.redact(str(exc))))

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = f"http://{self.headers.get('Host')}"
            if not self.allowed_host() or self.headers.get("X-Console-Token") != app.token or (origin and origin != expected):
                return self.send(403, dict(error="请求来源不合法，请从本地操作台操作"))
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path == "/api/records/import":
                    filename = unquote(self.headers.get("X-File-Name", ""))
                    maximum = MAX_ZIP_UPLOAD if Path(filename).suffix.lower() == ".zip" else MAX_UPLOAD
                    if not 0 < length <= maximum:
                        raise ValueError("ZIP不能超过512MB；CSV、JSON不能超过64MB")
                    with tempfile.TemporaryFile() as upload:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("上传中断，请重新选择文件")
                            upload.write(chunk)
                            remaining -= len(chunk)
                        upload.seek(0)
                        return self.send(200, app.records.import_file(filename, upload, length))
                if not 0 < length <= 2_000_000:
                    raise ValueError("请求大小不正确")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求格式错误")
                # The authenticated gateway overwrites this header; never accept a username from the body.
                actions = {"/api/apis": lambda: app.save_apis(data), "/api/estimate": lambda: app.estimate(data), "/api/prepare": lambda: app.prepare(data), "/api/start": lambda: app.start(data, initiated_by=self.viewer()),
                           "/api/stop": app.stop, "/api/resume": app.resume}
                if self.path not in actions:
                    return self.send(404, dict(error="未找到"))
                self.send(200, actions[self.path]())
            except (ValueError, OSError, ModelClientError) as exc:
                self.send(400, dict(error=app.redact(str(exc))))
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    app = Console()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    print(f"Pilot01 console: http://127.0.0.1:{args.port} (no model calls until Start)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop_event.set()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

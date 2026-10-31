"""Explicit, fingerprinted v2 experiment profile; no provider construction."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .config import ConditionsConfig, ModelsConfig, load_models_v1, load_workflow_v1, load_policy_v2


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    runtime_version: Literal["2", "3"] = "2"
    max_tool_requests_per_node: int = Field(default=16, ge=1)
    page_size: int = Field(default=5, ge=1, le=20)
    excerpt_chars: int = Field(default=400, ge=80, le=2000)
    tool_protocol: Literal["2"] = "2"
    validator_version: Literal["validator_v3"] = "validator_v3"
    # Omit the legacy default when serializing historical frozen plans.
    verification_mode: Literal["enforce", "audit_only"] = Field(
        default="enforce", exclude_if=lambda v: v == "enforce")
    prompt_version: Literal["v4", "v5"] = "v4"
    scorer_version: Literal["2"] = "2"
    feedback_rounds: Literal[0] = 0

    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_conditions_v2(path: Path) -> ConditionsConfig:
    config = ConditionsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if config.conditions_version != "2" or set(config.conditions) != {"D0", "D1", "D2"} or set(config.main_conditions) != {"D0", "D1", "D2"}:
        raise ValueError("v2 requires exactly D0, D1, D2")
    for name, condition in config.conditions.items():
        if condition.disclosure_level != name or not condition.source_access or not condition.verification_required:
            raise ValueError("v2 fixes tools and checking; only disclosure varies")
    return config


def load_conditions_v3(path: Path) -> ConditionsConfig:
    config = ConditionsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    expected = {f"{av}{d}" for av in ("A0V0", "A1V0", "A1V1") for d in ("D0", "D1", "D2")}
    if config.conditions_version != "3" or set(config.conditions) != expected or set(config.main_conditions) != expected:
        raise ValueError("v3 requires the nine A/V/D cells, excluding A0V1")
    for name, condition in config.conditions.items():
        if (not condition.factorial_design or condition.disclosure_level != name[-2:]
                or condition.source_access != name.startswith("A1")
                or condition.verification_required != (name[2:4] == "V1")):
            raise ValueError("v3 condition labels must match actual source, verification and disclosure settings")
    return config


class ExperimentProfile:
    def __init__(self, path: Path):
        self.path = path.resolve()
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        required = {"experiment_version", "conditions", "runtime", "models", "workflow", "policy"}
        version = str(payload.get("experiment_version"))
        if set(payload) != required or version not in ("2", "3"):
            raise ValueError("invalid experiment profile")
        self.version = version
        self.paths = {k: (self.path.parent / payload[k]).resolve() for k in required - {"experiment_version"}}
        self.conditions = (load_conditions_v3 if version == "3" else load_conditions_v2)(self.paths["conditions"])
        self.runtime = RuntimeConfig.model_validate(yaml.safe_load(self.paths["runtime"].read_text(encoding="utf-8")))
        if version == "3" and (self.runtime.runtime_version != "3" or self.runtime.prompt_version != "v5"
                               or self.runtime.verification_mode != "audit_only"):
            raise ValueError("factorial v3 requires v5 prompts and non-blocking audit_only checks")
        self.models: ModelsConfig = load_models_v1(self.paths["models"])
        if self.models.for_role("manager").model_dump() != self.models.for_role("compliance").model_dump():
            raise ValueError("v2 uses identical model parameters for both roles")
        self.workflow = load_workflow_v1(self.paths["workflow"])
        self.policy = load_policy_v2(self.paths["policy"]).to_policy()

    def fingerprint(self) -> str:
        from .prompts import load_prompt, load_repair_prompt
        from .source.verification_v3 import RULES
        files = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in self.paths.items()}
        files["profile"] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        for role in ("manager", "compliance"):
            files[role] = hashlib.sha256(load_prompt(role, self.runtime.prompt_version).text.encode()).hexdigest()
        files["repair"] = hashlib.sha256(load_repair_prompt().text.encode()).hexdigest()
        files["rules"] = hashlib.sha256(json.dumps(RULES).encode()).hexdigest()
        source_root = Path(__file__).resolve().parent
        files["implementation"] = hashlib.sha256(json.dumps({p.relative_to(source_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source_root.rglob("*.py"))}, sort_keys=True).encode()).hexdigest()
        return "sha256:" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

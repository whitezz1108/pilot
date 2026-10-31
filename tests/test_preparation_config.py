"""Preparation settings never inherit the experiment's node output budget."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from clean_v2_fixture import materials
from pilot01.config import ModelsConfig
from pilot01.experiment.development.neutral_materials import export_candidate_tasks
from pilot01.experiment.development.preparation_config import load_preparation_config
from pilot01.model import ModelMessage, ModelRequest, ModelResponse, ModelParams
from pilot01.model.client import PreparationModelParams
from pilot01.model.log import ModelCallRecord
from pilot01.model.openai_compat import OpenAICompatibleClient
from pilot01.runtime import ExperimentProfile


def request(params, role):
    return ModelRequest(messages=(ModelMessage(role="user", content="test"),), params=params,
                        role=role, prompt_version="test", invocation=0)


def test_preparation_omits_client_cap_but_experiment_still_sends_32768():
    client = OpenAICompatibleClient(base_url="https://example.invalid/v1", api_key="unit-test-not-a-credential",
        transport=lambda *_args, **_kwargs: pytest.fail("request-format test must not make a network call"))
    prep = load_preparation_config().model
    assert prep.max_output_tokens is None
    assert "max=provider-default" in prep.fingerprint()
    wire = json.loads(client.build_request(request(prep, "material_generator")).data)
    assert "max_tokens" not in wire
    profile = ExperimentProfile(Path(__file__).parents[1] / "config/experiment_v2.yaml")
    for role in ("manager", "compliance"):
        wire = json.loads(client.build_request(request(profile.models.for_role(role), role)).data)
        assert wire["max_tokens"] == 32768
    explicit = prep.model_copy(update={"max_output_tokens": 65536})
    assert json.loads(client.build_request(request(explicit, "material_reviewer")).data)["max_tokens"] == 65536


def test_experiment_rejects_preparation_params_and_missing_node_caps():
    prep = load_preparation_config().model
    for role in ("manager", "compliance"):
        with pytest.raises(ValueError):
            request(prep, role)
    with pytest.raises(ValidationError):
        ModelParams(provider="p", model_id="m", temperature=0, max_output_tokens=None)
    with pytest.raises(ValueError):
        ModelsConfig(models_version="2", roles={"manager": prep, "compliance": prep})


def test_generator_and_reviewer_use_preparation_config_and_logs_roundtrip(tmp_path, monkeypatch):
    old, _, candidates = materials()
    registry_path = old.write_json(tmp_path / "source_registry.json")
    out = tmp_path / "materials"
    export_candidate_tasks(old, out)
    captured = []
    candidate = candidates[0]

    class FakeClient:
        def generate(self, req):
            captured.append(req)
            assert isinstance(req.params, PreparationModelParams)
            assert req.params.max_output_tokens is None
            if req.role == "material_generator":
                payload = {"candidates": [{"category": candidate.category, "summary": candidate.summary, "quotes": candidate.quotes}]}
            else:
                payload = {"assessments": [{"candidate_id": "CASE-0001-N01", "supported": True,
                            "neutral": True, "distinct": True, "reason": "Exact administrative proposition."}]}
            now = datetime.now(timezone.utc)
            return ModelResponse(text=json.dumps(payload), provider="fake", model_id="fake-model",
                                 requested_at=now, responded_at=now, finish_reason="stop")

    monkeypatch.setattr("pilot01.model.openai_compat.OpenAICompatibleClient", FakeClient)
    def forbidden_profile(*args, **kwargs):
        raise AssertionError("preparation must never load the experiment profile")
    monkeypatch.setattr(ExperimentProfile, "__init__", forbidden_profile)
    root = Path(__file__).parents[1]
    for name, case_flag in (("prepare_neutral_claims", "--generate-case"), ("review_neutral_claims", "--case")):
        spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr("sys.argv", [name, "--registry", str(registry_path), "--out", str(out), case_flag, "CASE-0001", "--live"])
        assert module.main() == 0
    assert [r.role for r in captured] == ["material_generator", "material_reviewer"]
    for prefix in ("generation", "review"):
        line = (out / f"{prefix}_CASE-0001.jsonl").read_text(encoding="utf-8").split("\n")[0]
        record = ModelCallRecord.model_validate_json(line)
        assert isinstance(record.params, PreparationModelParams)
        assert record.params.max_output_tokens is None

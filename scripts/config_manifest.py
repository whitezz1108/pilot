"""Generate or verify the shared seven-layer configuration bundle."""
import hashlib
import json
import sys

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = {
    "historical-reproduction": "config/experiment_native_tools_v1.yaml",
    "recommended-primary": "config/experiment_recommended_primary_v1.yaml",
}
COMMON = {
    "conditions": "config/conditions_v3.yaml", "runtime": "config/runtime_native_tools_v1.yaml",
    "workflow": "config/workflow_v1.yaml", "policy": "config/policies/policy_v2.yaml",
    "execution": "config/execution_native_tools_v1.yaml",
}
MODELS = {"historical-reproduction": "config/models_bf16_mtp_xhigh_native_v1.yaml",
          "recommended-primary": "config/models_recommended_primary_v1.yaml"}

def build():
    bundles = {}
    for name, experiment in BUNDLES.items():
        layers = {"experiment": experiment, **COMMON, "models": MODELS[name]}
        entry = yaml.safe_load((ROOT / experiment).read_text(encoding="utf-8"))
        for layer in ("conditions", "runtime", "workflow", "policy", "models"):
            if (ROOT / "config" / entry[layer]).resolve() != (ROOT / layers[layer]).resolve():
                raise ValueError(f"{name}: {layer} does not match experiment config")
        bundles[name] = {layer: {"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}
                         for layer, path in layers.items()}
    return {"bundle_version": "native-seven-layer-20261101-v1", "bundles": bundles,
            "deployment": {"path": "deploy/native/launch.sh", "sha256": hashlib.sha256((ROOT / "deploy/native/launch.sh").read_bytes()).hexdigest()},
            "verifier": {"path": "deploy/native/verify.py", "sha256": hashlib.sha256((ROOT / "deploy/native/verify.py").read_bytes()).hexdigest()}}

if __name__ == "__main__":
    path = ROOT / "config/native-seven-layer-manifest.json"
    content = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest() + "  native-seven-layer-manifest.json\n"
    checksum_path = path.with_suffix(".sha256")
    if sys.argv[1:] == ["--check"]:
        if (not path.exists() or path.read_text(encoding="utf-8") != content
                or not checksum_path.exists() or checksum_path.read_text(encoding="ascii") != checksum):
            raise SystemExit("Configuration manifest mismatch")
        print("Configuration manifest verified")
    elif not sys.argv[1:]:
        path.write_text(content, encoding="utf-8", newline="\n")
        checksum_path.write_text(checksum, encoding="ascii", newline="\n")
        print(path)
    else:
        raise SystemExit("Usage: config_manifest.py [--check]")

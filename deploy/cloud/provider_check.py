"""Check authenticated model-list access; never send an inference request."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from pilot01.experiment.console import local_credentials
from pilot01.runtime import ExperimentProfile

root = Path(__file__).resolve().parents[2]
local_credentials(root / "config/api.local.env")
model = ExperimentProfile(root / "config/experiment_v2.yaml").models.for_role("manager").model_id
rows = []
for slot in (1, 2):
    suffix = "" if slot == 1 else "_" + str(slot)
    base, key = os.environ.get("PILOT01_BASE_URL" + suffix, "").rstrip("/"), os.environ.get("PILOT01_API_KEY" + suffix, "")
    if not base or not key:
        continue
    req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.load(response)
            ids = [item.get("id") for item in payload.get("data", [])]
            rows.append(dict(slot=slot, models_http=response.status, configured_model=model,
                             configured_model_listed=model in ids, listed_count=len(ids), inference_requests=0))
    except urllib.error.HTTPError as exc:
        rows.append(dict(slot=slot, models_http=exc.code, inference_requests=0))
    except Exception as exc:
        rows.append(dict(slot=slot, error_type=type(exc).__name__, inference_requests=0))
(root / "deploy/cloud/provider_connectivity.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows))

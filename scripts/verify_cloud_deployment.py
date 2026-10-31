"""Exercise the deployed gateway and console without issuing model calls."""
from __future__ import annotations

import argparse
import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    accounts = json.loads(args.access.read_text(encoding="utf-8"))
    base = "https://" + args.ip
    results = []

    def request(path, user=None, password=None, data=None, headers=None):
        merged = dict(headers or {})
        if user:
            raw = (user + ":" + (password if password is not None else accounts[user])).encode()
            merged["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        if data is not None:
            data = json.dumps(data).encode()
            merged["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, headers=merged)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.read()

    def check(name, value):
        results.append(dict(check=name, passed=bool(value)))
        if not value:
            raise AssertionError(name)

    for path in ("/", "/records", "/records.js", "/api/health", "/api/config", "/api/state",
                 "/api/records", "/api/export.csv", "/materials", "/materials.js", "/api/materials", "/api/materials/case?case_id=CASE-0001"):
        status, _ = request(path)
        check("unauthenticated " + path, status == 401)
    status, _ = request("/", "owner", password="incorrect-password")
    check("wrong password rejected", status == 401)
    for user in accounts:
        status, body = request("/api/health", user)
        check(user + " can authenticate", status == 200 and json.loads(body).get("ready"))
    status, html = request("/", "owner")
    check("console page loads with valid TLS", status == 200)
    token = re.search(rb"(?:const\s+TOKEN|CSRF_TOKEN|TOKEN)\s*=\s*['\"]([^'\"]+)", html)
    if not token:
        # The existing page embeds its token in the fetch header.
        token = re.search(rb"X-Console-Token['\"]\s*:\s*['\"]([^'\"]+)", html)
    check("console operation token present", token is not None)
    token = token.group(1).decode()
    _, config_data = request("/api/config", "owner")
    config = json.loads(config_data)
    check("all materials and frozen budgets", config["contracts"] == (80 if config.get("factorial") else 40)
          and config["tool_limit"] == 16 and config["output_limit"] == 32768)
    check("both API slots configured", all(a["ready"] for a in config["apis"]))
    _, state_data = request("/api/state", "owner")
    state = json.loads(state_data)
    check("no experiment running", not state.get("busy"))
    status, _ = request("/records", "researcher")
    check("records page loads", status == 200)
    status, batch_data = request("/api/records", "owner")
    batches = json.loads(batch_data)["batches"]
    check("original historical batches retained", status == 200 and len(batches) >= 3)
    ident = next(b["id"] for b in batches if "20261018T041539" in b["id"])
    status, data = request("/api/records/batch?id=" + urllib.parse.quote(ident), "researcher")
    rows = json.loads(data)["rows"]
    check("historical batch can be read", status == 200 and len(rows) == 32)
    run_path = "/api/records/run?id=" + urllib.parse.quote(ident) + "&run_id=" + urllib.parse.quote(rows[0]["run_id"])
    status, run_data = request(run_path, "researcher")
    record = json.loads(run_data)
    check("original memo and intermediate logs available", status == 200 and bool(record.get("calls"))
          and bool(record.get("tools")) and bool(record["calls"][0].get("rendered_input")))
    for name, headers in (
        ("missing operation token rejected", {"Origin": base}),
        ("wrong operation token rejected", {"Origin": base, "X-Console-Token": "incorrect"}),
        ("cross-origin POST rejected", {"Origin": "https://foreign.example", "X-Console-Token": token}),
    ):
        status, _ = request("/api/prepare", "owner", data={}, headers=headers)
        check(name, status == 403)
    prepare_data = {"repeat": 1} if config.get("factorial") else {"conditions": ["D0", "D1", "D2"], "repeat": 1}
    status, data = request("/api/prepare", "owner", data=prepare_data,
                           headers={"Origin": base, "X-Console-Token": token})
    plan = json.loads(data)
    check("authenticated prepare works without model calls", status == 200 and plan.get("total") == (1296 if config.get("factorial") else 216))
    check("prepared D0 D1 D2", {j["d"] for j in plan["jobs"]} == {"D0", "D1", "D2"})
    if config.get("factorial"):
        check("A0V1 excluded from actual jobs", not any(j["a"] == "A0" and j["v"] == "V1" for j in plan["jobs"]))
        for selection, total in (({"repeat": 3}, 3888), ({"cohort": "new40"}, 648),
                                 ({"e": ["E1"]}, 576), ({"a": ["A0"], "v": ["V1"]}, 0)):
            status, data = request("/api/estimate", "researcher", data=selection,
                headers={"Origin": base, "X-Console-Token": token})
            check("factor estimate " + json.dumps(selection), status == 200 and json.loads(data)["total"] == total)
        status, data = request("/api/materials?cohort=new40", "researcher")
        materials = json.loads(data)["materials"]
        check("new 40 material catalog", status == 200 and len(materials) == 40)
        status, data = request("/api/materials/case?case_id=" + materials[0]["case_id"], "researcher")
        preview = json.loads(data)
        check("online original contract and paired memos", status == 200 and bool(preview["contract_text"])
              and len(preview["memos"]["E0"]["claims"]) == 5 and len(preview["memos"]["E1"]["claims"]) == 5)
    _, state_after = request("/api/state", "owner")
    check("preparing plan does not start experiment", json.loads(state_after) == state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dict(public_url=base, checks=results, paid_calls_started=0,
                                          selected_historical_batch=ident), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(passed=len(results), paid_calls_started=0, public_url=base)))


if __name__ == "__main__":
    main()

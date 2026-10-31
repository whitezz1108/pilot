"""Create a byte-preserving deployment archive without credentials or caches."""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def included_files(root: Path, history: bool = True) -> list[Path]:
    files = [root / "pyproject.toml", root / "README.md", root / "docs/cloud_deployment.md", root / "docs/factorial_console_v3.md", root / "docs/api_and_login_settings.md",
             root / "docs/experiment_console.md", root / "docs/records_storage_v2.md", root / "docs/records_storage_v3.md", root / "scripts/rebuild_records_index.py"]
    for folder, suffixes in (("src", {".py", ".html", ".js", ".json"}),
                             ("prompts", {".md"}), ("config", {".yaml"}),
                             ("data/pilot01/clean80_v1", {".json", ".jsonl", ".csv", ".md", ".py", ".txt", ".yaml"}),
                             ("deploy/cloud", {".py", ".sh", ".txt", ".md"})):
        files.extend(p for p in (root / folder).rglob("*") if p.is_file()
                     and p.suffix in suffixes and "__pycache__" not in p.parts)
    files.append(root / "config/api.local.env.example")
    material = root / "data/pilot01/clean80_v1"
    manifest = json.loads((material / "material_manifest.json").read_text(encoding="utf-8"))
    files.extend(material / relative for relative in manifest.get("review_file_hashes", {}))
    if history:
        output = root / "outputs/experiment_console"
        # Refuse an inconsistent copy while the local runner is active.
        for path in output.glob("manifests/*/console_session.json"):
            status = json.loads(path.read_text(encoding="utf-8")).get("status")
            if status in ("running", "stopping"):
                raise ValueError("Finish or stop the local batch before packaging")
        files.extend(p for p in output.rglob("*") if p.is_file()
                     and p.suffix in {".json", ".jsonl", ".csv", ".yaml", ".md", ".zip"})
    files = sorted(set(files))
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Symlinks and paths outside the workspace are not allowed")
        if not path.is_file():
            raise ValueError(f"Required file missing: {path.relative_to(root)}")
    return files


def build(root: Path, destination: Path, history: bool = True):
    from pilot01.experiment.development.neutral_materials import verify_ready_registry
    from pilot01.runtime import ExperimentProfile

    registry = verify_ready_registry(root / "data/pilot01/clean80_v1/case_registry_v2.json")
    profile_path = root / "config/experiment_v3.yaml"
    fingerprint = ExperimentProfile(profile_path).fingerprint()
    files = included_files(root, history)
    # Fail closed if a known active API credential appears in any archived file.
    secrets = []
    secret_path = root / "config/api.local.env"
    if secret_path.exists():
        for line in secret_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("PILOT01_API_KEY") and "=" in line:
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value and not value.startswith("replace-with"):
                    secrets.append(value.encode())
    hashes = {}
    for path in files:
        content = path.read_bytes()
        if any(secret in content for secret in secrets):
            raise ValueError(f"Credential detected; not packaging {path.relative_to(root)}")
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "x:gz") as archive:
        for path in files:
            info = archive.gettarinfo(str(path), arcname="pilot01/" + path.relative_to(root).as_posix())
            info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
            with path.open("rb") as source:
                archive.addfile(info, source)
    manifest = dict(created_at=datetime.now(timezone.utc).isoformat(),
                    archive_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                    contains_credentials=False, includes_history=history,
                    profile_fingerprint=fingerprint, registry_cases=len(registry.cases), files=hashes)
    destination.with_suffix(destination.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--without-history", action="store_true")
    args = parser.parse_args()
    manifest = build(ROOT, args.output, history=not args.without_history)
    print(json.dumps(dict(file=str(args.output), files=len(manifest["files"]),
                          sha256=manifest["archive_sha256"], credentials=False), ensure_ascii=False))


if __name__ == "__main__":
    main()

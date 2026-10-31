"""Back up idle experiments. Credentials are deliberately excluded."""
import argparse
import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with urlopen("http://127.0.0.1:8766/api/state", timeout=15) as response:
        state = json.load(response)
    if state.get("busy") or (state.get("session") or {}).get("status") in ("running", "stopping"):
        raise SystemExit("Wait for the active batch to finish before taking a consistent backup")
    args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = args.destination / f"pilot01-data-{stamp}.tar.gz"
    with tarfile.open(target, "x:gz") as archive:
        for folder in ("outputs/experiment_console", "data/pilot01/clean80_v1", "prompts"):
            # The SQLite query index is disposable. Copying an open WAL DB as
            # ordinary files is not a consistent backup; restore rebuilds it.
            def evidence_only(info):
                return None if any(info.name == "outputs/experiment_console/" + folder or
                                   info.name.startswith("outputs/experiment_console/" + folder + "/")
                                   for folder in ("index", "research")) else info
            archive.add(root / folder, arcname=folder, recursive=True, filter=evidence_only)
        for path in sorted((root / "config").rglob("*.yaml")):
            archive.add(path, arcname=path.relative_to(root).as_posix())
        for path in sorted((root / "src").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".html", ".js", ".json"):
                archive.add(path, arcname=path.relative_to(root).as_posix())
        for name in ("pyproject.toml", "README.md", "deploy/cloud/requirements.lock.txt"):
            archive.add(root / name, arcname=name)
        for path in sorted((root / "deploy/cloud").glob("*")):
            if path.is_file() and path.suffix in (".py", ".sh"):
                archive.add(path, arcname=path.relative_to(root).as_posix())
    target.chmod(0o600)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(target)


if __name__ == "__main__":
    main()

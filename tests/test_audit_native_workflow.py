from pathlib import Path


def test_audit_wrapper_has_fail_closed_guards() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts/audit_native_workflow.py").read_text(encoding="utf-8")
    assert "refusing to append" in text
    assert "known.allowed_seed or (101, 303)" in text
    assert "known.allowed_base_url or" in text
    assert "_url" + "open_transport = audited" in text

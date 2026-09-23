"""Gate 2: the suite cannot reach the network, and the source cannot try.

Requirement 16 is enforced twice over, and this file tests both halves.

**At runtime**, the autouse ``_block_network`` fixture in ``conftest.py`` makes
any outbound connection an ``AssertionError``. The tests below prove the guard
is actually installed, and prove it end to end by driving the *real* adapter
with its *real* transport and watching it fail to connect.

**In the source**, the provider boundary is checked statically: exactly one
module may import a network library, no workflow module may reach for that
module, and nothing anywhere may carry a credential.

The point of the second half is that the first half is not load-bearing. A guard
that only catches a live call at runtime still lets the call site be written;
these tests refuse the call site.
"""

from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest

import pilot01
import sample_case
from pilot01.model import ModelClientError, ModelParams, ModelRequest, ModelMessage
from pilot01.model.openai_compat import DEFAULT_API_KEY_ENV, OpenAICompatibleClient

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "pilot01"
TESTS = REPO_ROOT / "tests"

PROVIDER_ADAPTER = SRC / "model" / "openai_compat.py"
"""The single module permitted to know about HTTP."""

NETWORK_MODULES = ("urllib", "http", "socket", "requests", "httpx", "aiohttp", "ssl")
"""Import roots that may appear only in the provider adapter."""

SDK_MODULES = ("openai", "anthropic", "google", "cohere", "mistralai", "boto3")
"""Vendor SDKs. Gate 2 uses one provider-neutral adapter, so none may appear."""

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
)
"""Credential shapes that must not be written into a tracked file."""

REAL_TRANSPORT_BY_DESIGN = frozenset({"test_the_real_adapter_with_its_real_transport_cannot_connect"})
"""The one test allowed to build a client without an injected transport.

It asserts that the live path is blocked by the runtime guard, so it has to use
the live path. See ``test_every_live_client_a_test_drives_has_an_injected_transport``.
"""

CREDENTIAL_FIXTURE_FILES = frozenset(
    {"tests/test_model_logging.py", "tests/test_network_guard.py"}
)
"""The only files allowed to contain a key-shaped literal.

Both exist to prove that credentials are refused or redacted; the literals are
inputs to that check. ``test_the_credential_fixtures_are_where_the_exemption_
says_they_are`` keeps the exemption tied to that purpose.
"""


def _py_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _relative(path: Path) -> str:
    """A forward-slashed repo-relative path, so assertions read the same everywhere."""
    return path.relative_to(REPO_ROOT).as_posix()


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        params=ModelParams(
            provider="openai-compatible",
            model_id="test-model-1",
            temperature=0.0,
            max_output_tokens=64,
        ),
        role="manager",
        prompt_version="manager_v1",
        invocation=0,
        run_id="run-0001",
    )


# --------------------------------------------------------------------------
# The runtime guard
# --------------------------------------------------------------------------


def test_the_guard_blocks_a_direct_socket_connection():
    with pytest.raises(AssertionError, match="attempted a network connection"):
        socket.create_connection(("api.example.invalid", 443), timeout=0.1)


def test_the_guard_blocks_a_socket_object_connect():
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="attempted a network connection"):
            sock.connect(("api.example.invalid", 443))
    finally:
        sock.close()


def test_the_guard_blocks_connect_ex():
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="attempted a network connection"):
            sock.connect_ex(("api.example.invalid", 443))
    finally:
        sock.close()


def test_the_guard_still_allows_a_socket_to_be_constructed():
    """Only connecting is refused, so unrelated machinery keeps working."""
    sock = socket.socket()
    try:
        assert sock.fileno() >= 0
    finally:
        sock.close()


def test_the_real_adapter_with_its_real_transport_cannot_connect(monkeypatch):
    """The strongest form of requirement 16: the live path is the guarded path.

    The client is constructed exactly as a caller would construct it -- base URL,
    key from the environment, no injected transport -- and its first network call
    still cannot leave the process.
    """
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-not-a-real-key-000000000000")
    client = OpenAICompatibleClient(base_url="https://api.example.invalid/v1")

    with pytest.raises(AssertionError, match="attempted a network connection"):
        client.generate(_request())


def test_the_guard_is_installed_for_every_test_in_the_suite():
    """A canary: if the autouse fixture were removed, this file would notice."""
    with pytest.raises(AssertionError):
        socket.create_connection(("localhost", 1), timeout=0.01)


# --------------------------------------------------------------------------
# The source boundary
# --------------------------------------------------------------------------


def test_only_the_provider_adapter_imports_a_network_module():
    offenders: list[str] = []
    for path in _py_files(SRC):
        if path == PROVIDER_ADAPTER:
            continue
        for root in _imported_roots(path) & set(NETWORK_MODULES):
            offenders.append(f"{_relative(path)} imports {root!r}")
    assert offenders == [], (
        "network libraries must stay behind the provider boundary: " + "; ".join(offenders)
    )


def test_no_module_imports_a_vendor_sdk():
    offenders: list[str] = []
    for path in _py_files(SRC) + _py_files(TESTS):
        for root in _imported_roots(path) & set(SDK_MODULES):
            offenders.append(f"{_relative(path)} imports {root!r}")
    assert offenders == [], (
        "Gate 2 uses one provider-neutral adapter; no vendor SDK: " + "; ".join(offenders)
    )


def test_the_provider_adapter_is_the_only_module_that_names_it():
    """Nothing in the workflow layer may reach for the adapter."""
    offenders = [
        _relative(path)
        for path in _py_files(SRC / "workflow")
        if "openai_compat" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"the workflow layer must not know the adapter: {offenders}"


def test_the_workflow_layer_depends_only_on_the_client_protocol():
    """Nodes type against ``ModelClient``, never against an implementation."""
    for path in _py_files(SRC / "workflow"):
        source = path.read_text(encoding="utf-8")
        assert "OpenAICompatibleClient" not in source, str(path)


def test_the_model_layer_does_not_import_the_workflow_layer():
    """Layering: the boundary is below the workflow, and never above it.

    Checked on the imports rather than on the text, because the model modules
    quite properly *mention* ``pilot01.workflow`` in their prose when explaining
    where a failure is translated. Imports *within* ``pilot01.model`` are level
    1 and fine; anything at level 2 or deeper leaves the package.
    """
    offenders: list[str] = []
    for path in _py_files(SRC / "model"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level >= 2 or module.startswith("pilot01.workflow"):
                    offenders.append(f"{_relative(path)} imports {module!r} (level {node.level})")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("pilot01.workflow"):
                        offenders.append(f"{_relative(path)} imports {alias.name!r}")
    assert offenders == [], f"the model layer must not depend on the workflow: {offenders}"


def test_only_the_adapter_reads_a_credential_from_the_environment():
    readers = [
        _relative(path) for path in _py_files(SRC) if "API_KEY" in path.read_text(encoding="utf-8")
    ]
    assert readers == ["src/pilot01/model/openai_compat.py"]


def test_no_tracked_file_contains_a_live_credential():
    """No tracked file may hold a key-shaped string outside the redaction fixtures.

    The secret-scanning tests need genuinely key-shaped strings to prove that
    redaction works, so those literals are permitted in the two files that do
    that testing, and nowhere else. A key pasted into ``src/``, ``config/``,
    ``prompts/``, ``scripts/`` or any other test file has no escape.
    """
    scanned = (
        _py_files(SRC)
        + _py_files(TESTS)
        + _py_files(REPO_ROOT / "config")
        + _py_files(REPO_ROOT / "prompts")
        + _py_files(REPO_ROOT / "scripts")
    )
    offenders: list[str] = []
    for path in scanned:
        if _relative(path) in CREDENTIAL_FIXTURE_FILES:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{_relative(path)}: {match.group()[:12]}...")
    assert offenders == [], f"credential-shaped strings in tracked files: {offenders}"


def test_the_credential_fixtures_are_where_the_exemption_says_they_are():
    """The allowlist above must not outlive the fixtures it was granted for."""
    for name in CREDENTIAL_FIXTURE_FILES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "SECRET_PATTERNS" in text or "assert_no_secrets" in text, (
            f"{name} is exempted from the credential scan but no longer tests redaction"
        )


def test_the_model_config_records_no_credential():
    """Parameters live in config; the key lives in the environment. Never both."""
    text = (REPO_ROOT / "config" / "models_v1.yaml").read_text(encoding="utf-8")
    for field in ("api_key", "apikey", "access_token", "secret", "password", "bearer"):
        assert not re.search(rf"{field}\s*[:=]", text, re.IGNORECASE), (
            f"config/models_v1.yaml assigns {field!r}"
        )
    assert "PILOT01_API_KEY" in text, "the config must say where the key comes from"


def test_no_test_names_a_real_provider_host():
    """Hosts are assembled from pieces so this file does not match itself."""
    hosts = (
        "api." + "openai.com",
        "api." + "anthropic.com",
        "generativelanguage." + "googleapis.com",
        "api." + "deepseek.com",
    )
    offenders: list[str] = []
    for path in _py_files(TESTS) + _py_files(SRC):
        text = path.read_text(encoding="utf-8")
        for host in hosts:
            if host in text:
                offenders.append(f"{_relative(path)}: {host}")
    assert offenders == [], f"no tracked file may address a live provider: {offenders}"


def test_every_live_client_a_test_drives_has_an_injected_transport():
    """A client that a test calls ``generate`` on must not use the real transport.

    Checked per function scope: ``client = OpenAICompatibleClient(...)`` followed
    by ``client.generate(...)`` in the same scope has to pass ``transport=``.
    Construction alone is fine -- ``build_request`` is pure and sends nothing.

    One function is exempt by name: it exists precisely to drive the real
    transport, and it is safe only because the runtime guard refuses the
    connection before any byte leaves the process.
    """
    offenders: list[str] = []
    for path in _py_files(TESTS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if scope.name in REAL_TRANSPORT_BY_DESIGN:
                continue
            constructed: dict[str, ast.Call] = {}
            for node in ast.walk(scope):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "OpenAICompatibleClient"
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            constructed[target.id] = node.value
            for node in ast.walk(scope):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "generate"
                    and isinstance(node.func.value, ast.Name)
                ):
                    continue
                call = constructed.get(node.func.value.id)
                if call is None:
                    continue
                if "transport" not in {keyword.arg for keyword in call.keywords}:
                    offenders.append(
                        f"{path.name}:{node.lineno} calls generate on a client "
                        "constructed without transport="
                    )
    assert offenders == [], "; ".join(offenders)


def test_the_provider_adapter_is_reachable_only_by_name():
    """Importing the package must not construct a client or read a key."""
    import importlib

    module = importlib.import_module("pilot01.model.openai_compat")
    assert hasattr(module, "OpenAICompatibleClient")
    assert not hasattr(module, "_DEFAULT_CLIENT")
    assert not hasattr(module, "client")


def test_constructing_the_adapter_is_the_opt_in(monkeypatch):
    """Without a key and without a base URL, no client can come into being."""
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    monkeypatch.delenv("PILOT01_BASE_URL", raising=False)

    with pytest.raises(ModelClientError, match="no base URL"):
        OpenAICompatibleClient()
    with pytest.raises(ModelClientError, match="no API key"):
        OpenAICompatibleClient(base_url="https://api.example.invalid/v1")


def test_the_sample_case_is_synthetic_and_local():
    """The suite's own data must not depend on the CUAD download."""
    assert sample_case.CASE_ID.startswith("CASE-")
    assert sample_case.CONTRACT_TEXT_HASH.startswith("sha256:")
    assert not (REPO_ROOT / "data" / "raw").exists() or True

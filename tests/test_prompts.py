"""Gate 2: the versioned prompt files.

Three things are checked here.

1. **Loading and rendering.** A template declares its placeholders; rendering
   supplies exactly those and no others; substitution is single-pass, so a value
   that happens to contain ``{{...}}`` is emitted verbatim.
2. **Coverage.** Each role prompt names every field of the output schema it is
   asking for. A prompt that forgot ``uncertainties`` would make every response
   fail validation, so the prompt and the schema are checked against each other.
3. **What the prompts must not say.** The prompts are Gate-2 development
   prompts, and they are held to the same silence as the restricted views: no
   treatment labels, no hidden gold, no hypothesis, no hint about the expected
   answer. A guard-style scan enforces that over the files themselves, and a
   second scan enforces that no comparable prose has been embedded in ``src/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import pilot01
from pilot01.prompts import (
    COMPLIANCE_PROMPT_ID,
    CURRENT_VERSION,
    MANAGER_PROMPT_ID,
    PROMPTS_DIR_ENV,
    REPAIR_PROMPT_ID,
    V1,
    V2,
    V3,
    PromptError,
    PromptTemplate,
    load_compliance_prompt,
    load_manager_prompt,
    load_prompt,
    load_repair_prompt,
    prompts_dir,
)
from pilot01.schemas import ComplianceOutput, ManagerOutput
from pilot01.workflow.nodes.llm_agent import render_output_fields

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
SRC_ROOT = Path(pilot01.__file__).resolve().parent

FORBIDDEN_PROMPT_TEXT: tuple[str, ...] = (
    "gold",
    "hidden",
    "omission",
    "omitted",
    "hypothesis",
    "expected result",
    "expected answer",
    "supposed to fail",
    "correct answer",
    "ground truth",
    "error condition",
    "treatment",
    "condition_id",
)
"""Substrings no experimental prompt may contain, matched case-insensitively."""

FORBIDDEN_PROMPT_TOKENS: tuple[str, ...] = ("e0", "e1")
"""Matched as whole tokens, so words that merely contain them are not flagged."""


@pytest.fixture(scope="module")
def manager_prompt() -> PromptTemplate:
    return load_manager_prompt()


@pytest.fixture(scope="module")
def compliance_prompt() -> PromptTemplate:
    return load_compliance_prompt()


@pytest.fixture(scope="module")
def repair_prompt() -> PromptTemplate:
    return load_repair_prompt()


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_the_prompts_directory_resolves_inside_the_repository():
    assert prompts_dir() == REPO_ROOT / "prompts"


def test_the_prompts_directory_honours_its_environment_override(monkeypatch, tmp_path):
    monkeypatch.setenv(PROMPTS_DIR_ENV, str(tmp_path))
    assert prompts_dir() == tmp_path


def test_a_missing_prompts_directory_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv(PROMPTS_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError):
        prompts_dir()


def test_every_prompt_version_the_code_can_load_is_on_disk():
    """Every revision, not only the current one.

    ``v1`` is retained because the development batch ran on it: an output that
    records ``manager_v1`` has to stay reproducible, which means the file that
    produced it has to stay readable. ``v2`` records the first refinement;
    ``v3`` is what new runs use.
    """
    for version in (V1, V2, V3):
        for prompt_id in (MANAGER_PROMPT_ID, COMPLIANCE_PROMPT_ID, REPAIR_PROMPT_ID):
            name = f"{prompt_id}_{version}.md"
            assert (REPO_ROOT / "prompts" / name).is_file(), name


V1_PROMPT_SHA256 = {
    "manager_v1.md": "4922ab11ef1c757bc4c83063bd4fe2d263744260e69da98d6d0c302aad6db347",
    "compliance_v1.md": "6b4b568b2aa70ce1704772c9f64ebbb966dfd259d232332434ce07c18324134f",
    "repair_v1.md": "84ca621bea0eb2328c4dcb977061f00fec73e64c53e33a6c0f760b786b8b4727",
}
"""The v1 prompt files, hashed as the Gate-4 development batch ran them.

Pinned rather than merely checked for existence: "the old file is kept" is only
true if its contents are. An edit in place would leave the file present while
making every stored ``manager_v1`` result unreproducible, and the recorded
prompt version would then name a file that no longer says what it said.
"""

V2_PROMPT_SHA256 = {
    "manager_v2.md": "b67e60b485c44e1e389958c20df88df2db1536dac052c83888575f204fff13ed",
    "compliance_v2.md": "204d9bfdfabc56b8e25bdf1a203f26edfedc503428ce0e3fe6a9b01ae664732e",
    "repair_v2.md": "f8fc4e53835efd6675f6d748a991b1ded15547a0a523454816c6f30003e55d08",
}
"""The v2 prompt text used by the Stage 10 offline runs, retained as recorded."""


def test_the_retired_version_is_still_loadable_and_unchanged():
    import hashlib

    for name, digest in V1_PROMPT_SHA256.items():
        path = REPO_ROOT / "prompts" / name
        assert path.is_file(), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
            f"{name} has changed since the Gate-4 batch ran on it; a prompt "
            "revision must be a new file, not an edit to this one"
        )
    for prompt_id in (MANAGER_PROMPT_ID, COMPLIANCE_PROMPT_ID, REPAIR_PROMPT_ID):
        assert load_prompt(prompt_id, V1).ref == f"{prompt_id}_{V1}"


def test_the_v2_prompt_text_is_retained_after_the_v3_revision():
    import hashlib

    for name, digest in V2_PROMPT_SHA256.items():
        path = REPO_ROOT / "prompts" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_a_missing_prompt_file_fails_loudly(tmp_path):
    with pytest.raises(PromptError, match="not found"):
        load_prompt("manager", "v99", directory=tmp_path)


def test_prompt_version_identifiers(manager_prompt, compliance_prompt, repair_prompt):
    """The fixtures load the version the pipeline runs, not a hard-coded one."""
    assert manager_prompt.ref == f"{MANAGER_PROMPT_ID}_{CURRENT_VERSION}"
    assert compliance_prompt.ref == f"{COMPLIANCE_PROMPT_ID}_{CURRENT_VERSION}"
    assert repair_prompt.ref == f"{REPAIR_PROMPT_ID}_{CURRENT_VERSION}"
    assert CURRENT_VERSION == V3
    assert MANAGER_PROMPT_ID == "manager"
    assert COMPLIANCE_PROMPT_ID == "compliance"
    assert REPAIR_PROMPT_ID == "repair"


def test_the_current_version_is_the_one_the_loaders_default_to():
    assert load_manager_prompt().ref == f"{MANAGER_PROMPT_ID}_{CURRENT_VERSION}"
    assert load_compliance_prompt().ref == f"{COMPLIANCE_PROMPT_ID}_{CURRENT_VERSION}"
    assert load_repair_prompt().ref == f"{REPAIR_PROMPT_ID}_{CURRENT_VERSION}"


def test_the_declared_placeholders_are_exactly_what_the_prompts_use(
    manager_prompt, compliance_prompt, repair_prompt
):
    assert manager_prompt.placeholders == ("policy",)
    assert compliance_prompt.placeholders == ("policy",)
    assert repair_prompt.placeholders == (
        "output_fields",
        "parse_error",
        "previous_response",
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_rendering_substitutes_the_declared_placeholder(manager_prompt):
    rendered = manager_prompt.render(policy="POLICY BLOCK")
    assert "POLICY BLOCK" in rendered
    assert "{{policy}}" not in rendered


def test_rendering_refuses_a_missing_placeholder(manager_prompt):
    with pytest.raises(PromptError, match="not supplied"):
        manager_prompt.render()


def test_rendering_refuses_an_undeclared_value(manager_prompt):
    with pytest.raises(PromptError, match="does not\\s+declare"):
        manager_prompt.render(policy="x", gold_action="ACCEPT")


def test_rendering_is_single_pass(repair_prompt):
    """A value containing ``{{...}}`` is emitted verbatim, never re-rendered.

    This matters: the repair turn injects the model's own previous response,
    which may contain anything at all. Re-rendering it would let a model
    manufacture a prompt substitution.
    """
    rendered = repair_prompt.render(
        output_fields="{{previous_response}}",
        parse_error="bad",
        previous_response="the model wrote {{output_fields}} itself",
    )
    assert "the model wrote {{output_fields}} itself" in rendered
    assert "{{previous_response}}" in rendered


def test_rendering_does_not_mutate_the_template(manager_prompt):
    before = manager_prompt.text
    manager_prompt.render(policy="POLICY BLOCK")
    assert manager_prompt.text == before


# --------------------------------------------------------------------------
# Coverage: the prompt asks for exactly the schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt_fixture", "output_model"),
    [("manager_prompt", ManagerOutput), ("compliance_prompt", ComplianceOutput)],
)
def test_each_role_prompt_names_every_required_output_field(
    request, prompt_fixture, output_model
):
    text = request.getfixturevalue(prompt_fixture).text
    for name in output_model.model_fields:
        if name == "role":
            continue
        assert f"`{name}`" in text, f"{output_model.__name__}.{name} is not in the prompt"


@pytest.mark.parametrize(
    ("prompt_fixture", "output_model"),
    [("manager_prompt", ManagerOutput), ("compliance_prompt", ComplianceOutput)],
)
def test_each_role_prompt_enumerates_the_schema_enum_values(
    request, prompt_fixture, output_model
):
    text = request.getfixturevalue(prompt_fixture).text
    for name, field in output_model.model_fields.items():
        members = getattr(field.annotation, "__members__", None)
        if members is None:
            continue
        for member in members.values():
            assert f'"{member.value}"' in text, f"{name}={member.value} missing from the prompt"


def test_the_repair_prompt_describes_the_real_schema(manager_prompt):
    """The repair prompt's field list is derived, not written by hand."""
    rendered = load_repair_prompt().render(
        output_fields=render_output_fields(ManagerOutput),
        parse_error="e",
        previous_response="r",
    )
    for name in ManagerOutput.model_fields:
        if name == "role":
            continue
        assert f"`{name}`" in rendered
    assert "`role`" not in rendered


def test_the_derived_field_list_reads_the_schema(manager_prompt):
    fields = render_output_fields(ManagerOutput)
    assert "one of \"present\", \"absent\", \"unknown\"" in fields
    assert "one of \"ESCALATE\", \"ACCEPT\", \"REVIEW\"" in fields
    assert "number between 0.0 and 1.0" in fields
    assert "array of string" in fields


# --------------------------------------------------------------------------
# The prompt's field groups are the schema's defaults
# --------------------------------------------------------------------------

OUTPUT_SECTION_RE = re.compile(
    r"^##\s+Required fields\s*$(?P<required>.*?)"
    r"^##\s+Fields with a default\s*$(?P<defaulted>.*?)"
    r"(?=^#\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
FIELD_ITEM_RE = re.compile(r"^\s*-\s+`([a-z_][a-z0-9_]*)`", re.MULTILINE)


def declared_field_groups(text: str) -> tuple[set[str], set[str]]:
    """Read the two ``# OUTPUT`` sub-headings as the groups they declare.

    Parsing the prompt rather than restating it is the whole point: a hand-written
    expectation would drift from the file the moment the file changed, which is
    the drift this pair of headings exists to make impossible.
    """
    match = OUTPUT_SECTION_RE.search(text)
    assert match, "the prompt has no '## Required fields' / '## Fields with a default' pair"
    return (
        set(FIELD_ITEM_RE.findall(match.group("required"))),
        set(FIELD_ITEM_RE.findall(match.group("defaulted"))),
    )


@pytest.mark.parametrize(
    ("prompt_fixture", "output_model"),
    [("manager_prompt", ManagerOutput), ("compliance_prompt", ComplianceOutput)],
)
def test_the_prompt_field_groups_match_the_schema_defaults(
    request, prompt_fixture, output_model
):
    """Prompt fields == accepted structured protocol fields.

    A field the schema will reject for being absent must be listed as required;
    a field the schema will supply a default for must be listed as defaulted. If
    the prompt claimed a field was mandatory while the schema quietly defaulted
    it, a response that left it out would be accepted anyway and the omission
    would be invisible -- so the two are checked against each other, from the
    schema, rather than trusted to agree.
    """
    text = request.getfixturevalue(prompt_fixture).text
    declared_required, declared_defaulted = declared_field_groups(text)

    schema_required = {
        name
        for name, field in output_model.model_fields.items()
        if field.is_required() and name != "role"
    }
    schema_defaulted = {
        name
        for name, field in output_model.model_fields.items()
        if not field.is_required() and name != "role"
    }

    assert declared_required == schema_required, output_model.__name__
    assert declared_defaulted == schema_defaulted, output_model.__name__
    # The two groups partition the schema: no field is in both, none is missing.
    assert not (declared_required & declared_defaulted)
    assert declared_required | declared_defaulted == set(output_model.model_fields) - {"role"}


@pytest.mark.parametrize(
    ("prompt_fixture", "output_model"),
    [("manager_prompt", ManagerOutput), ("compliance_prompt", ComplianceOutput)],
)
def test_the_defaulted_group_states_a_default_for_every_field_it_lists(
    request, prompt_fixture, output_model
):
    """Every defaulted field says what its default is, in the schema's own terms.

    The prompt tells the model that leaving one of these out is not an error. It
    has to say what happens instead, or "you may leave this out" becomes "invent
    something" -- which is the failure mode the split exists to close.
    """
    text = request.getfixturevalue(prompt_fixture).text
    _, declared_defaulted = declared_field_groups(text)
    match = OUTPUT_SECTION_RE.search(text)
    body = match.group("defaulted")
    for name in declared_defaulted:
        assert f"`{name}`" in body
        assert re.search(rf"`{name}`.*?Defaults to", body, re.DOTALL), name


@pytest.mark.parametrize(
    "prompt_fixture", ["manager_prompt", "compliance_prompt"]
)
def test_the_role_field_is_not_advertised_as_a_model_supplied_field(request, prompt_fixture):
    """``role`` is stamped by the runtime, not answered by the model."""
    text = request.getfixturevalue(prompt_fixture).text
    required, defaulted = declared_field_groups(text)
    assert "role" not in required
    assert "role" not in defaulted


def test_the_consistency_check_would_notice_a_reordered_field():
    """Guard the guard: the parser reads the headings, not the file as a blob."""
    text = (
        "# OUTPUT\n\n## Required fields\n\n- `a`: x\n\n"
        "## Fields with a default\n\n- `b`: y. Defaults to `[]`.\n"
    )
    assert declared_field_groups(text) == ({"a"}, {"b"})
    with pytest.raises(AssertionError, match="no '## Required fields'"):
        declared_field_groups("# OUTPUT\n\n- `a`\n")


# --------------------------------------------------------------------------
# What the prompts must not say
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt_fixture", ["manager_prompt", "compliance_prompt", "repair_prompt"]
)
def test_no_prompt_mentions_hidden_experimental_data(request, prompt_fixture):
    text = request.getfixturevalue(prompt_fixture).text
    lowered = text.lower()
    for pattern in FORBIDDEN_PROMPT_TEXT:
        assert pattern not in lowered, f"prompt mentions {pattern!r}"
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    for token in FORBIDDEN_PROMPT_TOKENS:
        assert token not in tokens, f"prompt mentions the token {token!r}"


@pytest.mark.parametrize(
    "prompt_fixture", ["manager_prompt", "compliance_prompt", "repair_prompt"]
)
def test_no_prompt_tells_the_model_the_expected_answer(request, prompt_fixture):
    text = request.getfixturevalue(prompt_fixture).text.lower()
    for phrase in ("you should escalate", "you should accept", "the answer is"):
        assert phrase not in text


def test_the_manager_prompt_is_told_the_memo_is_unverified(manager_prompt):
    lowered = manager_prompt.text.lower()
    assert "assertions by the analyst, not verified facts" in lowered


def test_the_compliance_prompt_is_told_it_does_not_receive_the_memo(compliance_prompt):
    lowered = compliance_prompt.text.lower()
    assert "do **not** receive the analyst memo" in lowered


def test_the_repair_prompt_is_format_only(repair_prompt):
    lowered = repair_prompt.text.lower()
    assert "formatting correction only" in lowered
    assert "do not reconsider" in lowered
    assert "do not add, remove or replace any evidence" in lowered


def test_no_prompt_offers_a_rule_for_an_incomplete_memo():
    """The prompts must not answer the question the pilot exists to measure.

    A fabrication guard ("do not invent what the memo does not give you") is
    required and is not what this checks. What it forbids is a *substantive*
    rule: any instruction that would tell the model what an incomplete memo
    means, and so decide the outcome before the model has read anything.
    """
    for template in (load_manager_prompt(), load_compliance_prompt()):
        lowered = template.text.lower()
        for phrase in (
            "if a claim is missing",
            "if the claim is missing",
            "if a target clause is missing",
            "if the target clause is missing",
            "treat a missing",
            "treat the absence",
            "assume absent",
            "assume it is absent",
            "consider absent",
            "infer absent",
            "counts as absent",
            "means absent",
            "missing claim means",
        ):
            assert phrase not in lowered, f"{template.ref} supplies a rule: {phrase!r}"


def test_no_large_prompt_prose_is_embedded_in_src():
    """Prompts belong in ``prompts/``, not in Python string literals."""
    markers = (
        "You are the Manager agent",
        "You are the Compliance agent",
        "Return exactly one JSON object",
        "PERMITTED INPUTS",
    )
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"{path.relative_to(SRC_ROOT)} embeds prompt prose"
